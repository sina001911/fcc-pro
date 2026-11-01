"""VideoJob dataclass for the media job system."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VideoJob:
    """Immutable record representing one video-generation job."""

    job_id: str
    provider_id: str
    provider_model: str
    status: str  # "pending" | "processing" | "succeeded" | "failed" | "expired"
    request_body: dict
    result_media_url: str | None
    error: str | None
    created_at: int
    updated_at: int
    expires_at: int  # unix timestamp


__all__ = ["VideoJob"]
