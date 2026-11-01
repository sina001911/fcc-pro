"""Image generation API handler for OpenAI-compatible image endpoints."""

from collections.abc import Mapping

import httpx
from fastapi.responses import JSONResponse, Response

from free_claude_code.api.request_ids import new_request_id
from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.application.media_capability import (
    is_image_generation_provider,
    is_model_enabled_for_image,
)
from free_claude_code.application.routing import ModelRouter
from free_claude_code.config.api_keys import selected_api_key
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.diagnostics import safe_exception_message
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.openai_responses import openai_error_payload


class ImagesHandler:
    """Handle image generation requests by forwarding to upstream providers."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_router: ModelRouter | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings)
        self._request_headers = request_headers

    async def generate(
        self, request_data: JsonObject, *, request_id: str | None = None
    ) -> Response:
        """Generate an image by forwarding to an upstream provider."""
        request_id = request_id or new_request_id()

        # --- validate required fields ---
        model = request_data.get("model")
        if not isinstance(model, str) or not model.strip():
            raise InvalidRequestError("Image generation model must not be empty.")

        prompt = request_data.get("prompt")
        if prompt is None or (isinstance(prompt, str) and not prompt.strip()):
            raise InvalidRequestError("Image generation prompt must not be empty.")

        # --- validate optional fields ---
        n = request_data.get("n")
        if n is not None and (not isinstance(n, int) or n < 1 or n > 10):
            raise InvalidRequestError(
                "Image generation n must be an integer between 1 and 10."
            )

        size = request_data.get("size")
        if size is not None and not isinstance(size, str):
            raise InvalidRequestError("Image generation size must be a string.")

        response_format = request_data.get("response_format")
        if response_format is not None and response_format not in ("url", "b64_json"):
            raise InvalidRequestError(
                "Image generation response_format must be 'url' or 'b64_json'."
            )

        # --- resolve model to provider ---
        resolved = self._model_router.resolve(model)
        provider_id = resolved.primary.provider_id
        provider_model = resolved.primary.provider_model

        descriptor = PROVIDER_CATALOG.get(provider_id)
        if descriptor is None:
            raise InvalidRequestError(f"Unknown provider: {provider_id}")
        if not descriptor.image_generation:
            raise InvalidRequestError(
                f"Provider {provider_id!r} does not support image generation. "
                "Enable image_generation on the provider descriptor to allow this."
            )
        if not is_model_enabled_for_image(
            resolved.primary.provider_model_ref, self._settings
        ):
            raise InvalidRequestError(
                f"Model {provider_model!r} on provider {provider_id!r} is not "
                "enabled for image generation. Enable it in the Media Models "
                "admin panel or use a model that is already verified."
            )

        # --- resolve API key ---
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

        # --- resolve base URL ---
        base_url = descriptor.default_base_url
        if descriptor.base_url_attr:
            custom_url = getattr(self._settings, descriptor.base_url_attr, None)
            if custom_url:
                base_url = custom_url
        if base_url is None:
            raise InvalidRequestError(
                f"Provider {provider_id!r} has no base URL configured."
            )

        # --- build upstream payload ---
        payload = dict(request_data)
        payload["model"] = provider_model

        normalized_base = base_url.rstrip("/")
        if not normalized_base.endswith("/v1"):
            normalized_base = f"{normalized_base}/v1"
        url = f"{normalized_base}/images/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._settings.http_read_timeout)

        # --- forward to upstream ---
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException:
            raise InvalidRequestError("Image generation request timed out.")
        except httpx.NetworkError as exc:
            raise InvalidRequestError(
                f"Failed to connect to image generation provider: "
                f"{safe_exception_message(exc)}"
            )

        # --- handle upstream errors ---
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = {"error": {"message": response.text}}
            return JSONResponse(
                status_code=response.status_code,
                content=openai_error_payload(
                    message=error_body.get("error", {}).get(
                        "message", "Image generation failed"
                    ),
                    error_type="api_error",
                ),
            )

        # --- return image response ---
        if not response.content:
            return JSONResponse(
                status_code=502,
                content=openai_error_payload(
                    message="Empty response from image generation provider",
                    error_type="api_error",
                ),
            )

        content_type = response.headers.get("content-type", "image/png")
        if "json" in content_type.lower():
            return JSONResponse(
                status_code=response.status_code,
                content=response.json(),
            )
        return Response(
            content=response.content,
            media_type=content_type,
        )
