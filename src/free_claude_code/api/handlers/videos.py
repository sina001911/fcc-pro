"""Video generation API handler for OpenAI-compatible video endpoints."""

from collections.abc import Mapping

from fastapi.responses import JSONResponse, Response

from free_claude_code.api.request_ids import new_request_id
from free_claude_code.application.errors import ApplicationError, InvalidRequestError
from free_claude_code.application.media_capability import (
    is_model_enabled_for_video,
    is_video_generation_provider,
)
from free_claude_code.application.media_jobs.store import MediaJobStore
from free_claude_code.application.routing import ModelRouter
from free_claude_code.config.api_keys import selected_api_key
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.diagnostics import safe_exception_message
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.openai_responses import openai_error_payload


class VideosHandler:
    """Handle video generation requests by managing async jobs."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_router: ModelRouter | None = None,
        request_headers: Mapping[str, str] | None = None,
        media_job_store: MediaJobStore | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings)
        self._request_headers = request_headers
        self._media_job_store = media_job_store

    async def create(
        self, request_data: JsonObject, *, request_id: str | None = None
    ) -> Response:
        """Submit a video-generation job and return 202 with the job id."""
        request_id = request_id or new_request_id()

        model = request_data.get("model")
        if not isinstance(model, str) or not model.strip():
            raise InvalidRequestError("Video generation model must not be empty.")

        prompt = request_data.get("prompt")
        if prompt is None or (isinstance(prompt, str) and not prompt.strip()):
            raise InvalidRequestError("Video generation prompt must not be empty.")

        resolved = self._model_router.resolve(model)
        provider_id = resolved.primary.provider_id
        provider_model = resolved.primary.provider_model

        descriptor = PROVIDER_CATALOG.get(provider_id)
        if descriptor is None:
            raise InvalidRequestError(f"Unknown provider: {provider_id}")
        if not descriptor.video_generation:
            raise InvalidRequestError(
                f"Provider {provider_id!r} does not support video generation. "
                "Enable video_generation on the provider descriptor to allow this."
            )
        if not is_model_enabled_for_video(
            resolved.primary.provider_model_ref, self._settings
        ):
            raise InvalidRequestError(
                f"Model {provider_model!r} on provider {provider_id!r} is not "
                "enabled for video generation. Enable it in the Media Models "
                "admin panel or use a model that is already verified."
            )

        api_key: str | None = None
        if descriptor.credential_attr:
            keys = getattr(self._settings, descriptor.credential_attr, ())
            api_key = selected_api_key(keys, resolved.primary.api_key_number)
        if api_key is None and descriptor.static_credential:
            api_key = descriptor.static_credential
        if api_key is None:
            raise InvalidRequestError(
                f"Provider {provider_id!r} has no API key configured."
            )

        base_url = descriptor.default_base_url
        if descriptor.base_url_attr:
            custom_url = getattr(self._settings, descriptor.base_url_attr, None)
            if custom_url:
                base_url = custom_url
        if base_url is None:
            raise InvalidRequestError(
                f"Provider {provider_id!r} has no base URL configured."
            )

        payload = dict(request_data)
        payload["model"] = provider_model

        normalized_base = base_url.rstrip("/")
        if not normalized_base.endswith("/v1"):
            normalized_base = f"{normalized_base}/v1"
        url = f"{normalized_base}/videos/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        import httpx
        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self._settings.http_read_timeout),
            write=10.0,
            pool=10.0,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise InvalidRequestError("Video generation request timed out.")
        except httpx.NetworkError as exc:
            raise InvalidRequestError(
                f"Failed to connect to video generation provider: "
                f"{safe_exception_message(exc)}"
            )

        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = {"error": {"message": response.text}}
            return JSONResponse(
                status_code=response.status_code,
                content=openai_error_payload(
                    message=error_body.get("error", {}).get(
                        "message", "Video generation failed"
                    ),
                    error_type="api_error",
                ),
            )

        upstream = response.json()
        job_id = upstream.get("job_id")
        if not job_id:
            return JSONResponse(
                status_code=502,
                content=openai_error_payload(
                    message="No job id returned from video generation provider",
                    error_type="api_error",
                ),
            )

        # Persist the accepted job so poll() can resolve its real status.
        # Store it under the upstream-provided job_id so that the identifier
        # returned to clients in this 202 response is the same one poll()
        # (and the background poller) will look up.
        if self._media_job_store is not None:
            try:
                await self._media_job_store.create_job(
                    provider_id=provider_id,
                    provider_model=provider_model,
                    request_body=request_data,
                    job_id=job_id,
                )
            except Exception:
                # Best-effort persistence: the upstream accepted the job and
                # the poller will still recover it from the upstream provider.
                pass

        return JSONResponse(
            status_code=202,
            content={
                "job_id": job_id,
                "status": "pending",
                "provider": provider_id,
                "model": provider_model,
            },
        )

    async def poll(self, job_id: str, *, request_id: str | None = None) -> Response:
        """Return the current status of a video-generation job.

        Reads the requested job from the configured MediaJobStore. Unknown job
        ids return a deterministic 404; store failures return a 500. Successful
        polls echo the persisted status plus any output metadata the store has
        recorded, and never fabricate a result for a job that has not completed.
        """
        request_id = request_id or new_request_id()
        store = self._media_job_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content=openai_error_payload(
                    message="Media job store is not available.",
                    error_type="server_error",
                ),
            )
        try:
            job = await store.get_job(job_id)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=openai_error_payload(
                    message=f"Media job lookup failed: {safe_exception_message(exc)}",
                    error_type="internal_error",
                ),
            )
        if job is None:
            return JSONResponse(
                status_code=404,
                content=openai_error_payload(
                    message=f"Video generation job not found: {job_id}",
                    error_type="not_found",
                ),
            )
        content: JsonObject = {
            "job_id": job.job_id,
            "status": job.status,
            "provider": job.provider_id,
            "model": job.provider_model,
        }
        if job.status == "succeeded" and job.result_media_url is not None:
            content["result_media_url"] = job.result_media_url
        if job.error is not None:
            content["error"] = job.error
        return JSONResponse(status_code=200, content=content)


__all__ = ["VideosHandler"]