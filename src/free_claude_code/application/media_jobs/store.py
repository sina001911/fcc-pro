"""SQLite-backed store for video generation jobs."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Optional

import sqlite3

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.runtime.sqlite_database import SQLiteDatabase

from .models import VideoJob

_VALID_STATUSES = frozenset({"pending", "processing", "succeeded", "failed", "expired"})
_STATUS_TRANSITIONS = {
    "pending": {"processing", "failed", "expired"},
    "processing": {"succeeded", "failed", "expired"},
    "succeeded": set(),
    "failed": set(),
    "expired": set(),
}


class MediaJobStore:
    """SQLite-backed store for video-generation jobs."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._lock = asyncio.Lock()

    async def create_job(
        self,
        *,
        provider_id: str,
        provider_model: str,
        request_body: dict,
        ttl_seconds: int = 3600,
        job_id: str | None = None,
    ) -> tuple[VideoJob, str]:
        """Insert a new pending job and return (job, job_id).

        If ``job_id`` is supplied it is stored verbatim so that callers can
        persist a job under the identifier they already returned to clients
        (e.g. an upstream provider's job id). When omitted, a fresh
        ``secrets.token_hex(16)`` identifier is generated.
        """
        if job_id is None:
            job_id = secrets.token_hex(16)
        now = int(time.time())
        expires_at = now + max(60, int(ttl_seconds))
        body_json = json.dumps(request_body, ensure_ascii=False, separators=(",", ":"))

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT OR REPLACE INTO media_jobs
                    (job_id, provider_id, provider_model, status,
                     request_body, result_media_url, error,
                     created_at, updated_at, expires_at)
                VALUES (?, ?, ?, 'pending', ?, NULL, NULL, ?, ?, ?)
                """,
                (job_id, provider_id, provider_model, body_json, now, now, expires_at),
            )

        async with self._lock:
            await self._database.run(insert)

        job = VideoJob(
            job_id=job_id,
            provider_id=provider_id,
            provider_model=provider_model,
            status="pending",
            request_body=request_body,
            result_media_url=None,
            error=None,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        return job, job_id

    async def get_job(self, job_id: str) -> VideoJob | None:
        def load(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return connection.execute(
                "SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()

        async with self._lock:
            row = await self._database.run(load, write=False)
            if row is None:
                return None
            return self._row_to_job(row)

    async def update_status(
        self,
        job_id: str,
        new_status: str,
        *,
        media_url: str | None = None,
        error: str | None = None,
    ) -> VideoJob | None:
        """Transition a job to a new status; returns updated job or None."""
        if new_status not in _VALID_STATUSES:
            raise InvalidRequestError(f"Invalid status: {new_status}")

        def update(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            row = connection.execute(
                "SELECT status FROM media_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            current_status = row[0]
            if new_status not in _STATUS_TRANSITIONS.get(current_status, set()):
                raise InvalidRequestError(
                    f"Cannot transition from '{current_status}' to '{new_status}'."
                )
            now = int(time.time())
            connection.execute(
                """
                UPDATE media_jobs SET status=?, updated_at=?,
                    result_media_url=?, error=?
                WHERE job_id=?
                """,
                (new_status, now, media_url, error, job_id),
            )
            return connection.execute(
                "SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()

        async with self._lock:
            row = await self._database.run(update)
            if row is None:
                return None
            return self._row_to_job(row)

    async def scan_expired(self, now: int | None = None) -> int:
        """Mark all pending/processing jobs whose expires_at < now as 'expired'."""
        if now is None:
            now = int(time.time())

        def expire(connection: sqlite3.Connection) -> int:
            cur = connection.execute(
                """
                UPDATE media_jobs
                SET status='expired', updated_at=?
                WHERE expires_at < ? AND status IN ('pending','processing')
                """,
                (now, now),
            )
            return cur.rowcount

        async with self._lock:
            return await self._database.run(expire)

    async def scan_processing(self, now: int | None = None) -> list[tuple[str, str, str]]:
        """Return list of (job_id, provider_id, request_body_json) for processing jobs."""
        def load(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
            return list(connection.execute(
                "SELECT job_id, provider_id, request_body FROM media_jobs WHERE status='processing'"
            ))

        async with self._lock:
            return await self._database.run(load, write=False)

    def _row_to_job(self, row) -> VideoJob:
        return VideoJob(
            job_id=row["job_id"],
            provider_id=row["provider_id"],
            provider_model=row["provider_model"],
            status=row["status"],
            request_body=json.loads(row["request_body"]),
            result_media_url=row["result_media_url"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )


    async def close(self) -> None:
        """No-op: the store shares the application's SQLiteDatabase.

        Kept for symmetry with the poller's start/stop lifecycle so callers
        can invoke ``await store.close()`` during shutdown without guarding.
        """

    async def recover_stuck_jobs(self, *, now: int | None = None) -> int:
        """Mark any jobs stuck in 'processing' that exceed their TTL as 'failed'."""
        if now is None:
            now = int(time.time())

        def recover(connection) -> int:
            cur = connection.execute(
                """
                UPDATE media_jobs
                SET status='failed', error='job timed out while processing',
                    updated_at=?
                WHERE status='processing' AND expires_at < ?
                """,
                (now, now),
            )
            return cur.rowcount

        async with self._lock:
            return await self._database.run(recover)


__all__ = ["MediaJobStore"]