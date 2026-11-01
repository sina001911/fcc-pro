"""Background poller that advances video-generation jobs."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from free_claude_code.application.media_jobs.store import MediaJobStore
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings

logger = logging.getLogger(__name__)
POLL_INTERVAL = 30  # seconds


class MediaJobPoller:
    """Periodically polls 'processing' video jobs against upstream providers."""

    def __init__(
        self,
        store: MediaJobStore,
        settings: Settings,
    ) -> None:
        self._store = store
        self._settings = settings
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Launch the background polling loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="fcc-media-job-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("MediaJobPoller tick failed: %s", exc)

    async def _poll_once(self) -> None:
        await self._store.scan_expired()
        jobs = await self._store.scan_processing()
        if not jobs:
            return
        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self._settings.http_read_timeout),
            write=10.0,
            pool=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            for job_id, provider_id, request_body_json in jobs:
                await self._check_one(client, job_id, provider_id, request_body_json)

    async def _check_one(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        provider_id: str,
        request_body_json: str,
    ) -> None:
        import json as _json
        descriptor = PROVIDER_CATALOG.get(provider_id)
        if descriptor is None or not descriptor.video_generation:
            await self._store.update_status(job_id, "failed", error="Provider unsupported")
            return

        api_key = self._resolve_api_key(descriptor)
        base_url = self._resolve_base_url(descriptor)
        if not api_key or not base_url:
            await self._store.update_status(job_id, "failed", error="Missing provider config")
            return

        url = f"{base_url.rstrip('/')}/videos/generations/{job_id}"
        try:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException:
            return  # still processing
        except httpx.NetworkError:
            await self._store.update_status(job_id, "failed", error="Network error")
            return

        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            if status == "succeeded":
                media_url = data.get("media_url") or data.get("video_url")
                await self._store.update_status(
                    job_id, "succeeded", media_url=media_url
                )
            elif status == "failed":
                error = data.get("error", "Unknown failure")
                await self._store.update_status(job_id, "failed", error=error)
            # else: still processing, do nothing
        elif resp.status_code == 404:
            await self._store.update_status(job_id, "failed", error="Upstream job not found")
        else:
            await self._store.update_status(job_id, "failed", error=f"HTTP {resp.status_code}")

    def _resolve_api_key(self, descriptor) -> Optional[str]:
        keys = getattr(self._settings, descriptor.credential_attr, ()) if descriptor.credential_attr else ()
        from free_claude_code.config.api_keys import selected_api_key
        return selected_api_key(keys, 0) or descriptor.static_credential

    def _resolve_base_url(self, descriptor) -> Optional[str]:
        url = descriptor.default_base_url
        if descriptor.base_url_attr:
            custom = getattr(self._settings, descriptor.base_url_attr, None)
            if custom:
                url = custom
        return url


__all__ = ["MediaJobPoller", "POLL_INTERVAL"]