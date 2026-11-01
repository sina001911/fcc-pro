"""Media job queue for video generation."""

import sqlite3


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS media_jobs (
            job_id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            provider_model TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','processing','succeeded','failed','expired')),
            request_body TEXT NOT NULL,
            result_media_url TEXT,
            error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_jobs_status ON media_jobs(status, expires_at)"
    )