"""Artifact upload/download API handler (P4).

Uploads persist bytes to a filesystem store and return a signed download
link; downloads require the matching signed token. Expired tokens return
410 Gone; unknown/tampered tokens return 404 Not Found (no existence oracle).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

from free_claude_code.api.request_ids import new_request_id
from free_claude_code.application.artifacts.store import ArtifactStore
from free_claude_code.application.errors import (
    ApplicationError,
    ApplicationUnavailableError,
    InvalidRequestError,
)
from free_claude_code.config.paths import config_dir_path
from free_claude_code.config.settings import Settings
from free_claude_code.core.diagnostics import safe_exception_message
from free_claude_code.core.json_types import JsonObject

_DEFAULT_ARTIFACT_DIRNAME = "artifacts"


def _artifact_root(settings: Settings) -> tuple[Path, bool]:
    """Return (root, configured). `configured` is True only when ARTIFACT_DIR
    was explicitly set, so the store's world-writable check (I-4) applies to
    custom locations and leaves the default ~/.fcc/artifacts untouched."""
    configured = settings.artifact_dir
    if configured:
        return Path(configured), True
    return config_dir_path() / _DEFAULT_ARTIFACT_DIRNAME, False


_MIN_SIGNING_KEY_BYTES = 16


def _signing_key(settings: Settings) -> bytes:
    secret = settings.artifact_signing_secret
    if not secret:
        raise InvalidRequestError("Artifact signing secret is not configured.")
    if isinstance(secret, str):
        key = secret.encode("utf-8")
    else:
        key = bytes(secret)
    # I-3: reject keys too short to be safe HMAC secrets. This is a request-
    # time check (never runs at startup) and does not affect any existing
    # valid secret.
    if len(key) < _MIN_SIGNING_KEY_BYTES:
        raise InvalidRequestError(
            "Artifact signing secret must be at least 16 bytes."
        )
    return key


class ArtifactsHandler:
    """Handle artifact upload and signed download."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        root, configured = _artifact_root(settings)
        self._store = ArtifactStore(
            root,
            max_bytes=settings.artifact_max_bytes,
            configured=configured,
        )

    async def create(
        self,
        request_data: Mapping[str, object],
        *,
        request_id: str | None = None,
    ) -> JSONResponse:
        """Persist an artifact and return 201 with its signed download link."""
        request_id = request_id or new_request_id()
        try:
            data_field = request_data.get("data")
            if isinstance(data_field, str):
                try:
                    data = base64.b64decode(data_field, validate=True)
                except Exception as exc:
                    raise InvalidRequestError(
                        "Artifact data must be valid base64."
                    ) from exc
            elif isinstance(data_field, (bytes, bytearray)):
                data = bytes(data_field)
            else:
                raise InvalidRequestError("Artifact data must be base64 or bytes.")
            filename = request_data.get("filename", "artifact.bin")
            content_type = request_data.get("content_type", "application/octet-stream")
            kind = request_data.get("kind", "raw")
            source_format = request_data.get("source_format")
            metadata = request_data.get("metadata")
            ttl_seconds = int(
                request_data.get("ttl_seconds", self._settings.artifact_link_ttl_seconds)
            )
            signing_key = _signing_key(self._settings)
            record, link = await self._store.store(
                data=data,
                filename=str(filename),
                content_type=str(content_type),
                kind=str(kind),
                source_format=str(source_format) if source_format is not None else None,
                metadata=metadata if isinstance(metadata, dict) else None,
                ttl_seconds=ttl_seconds,
                signing_key=signing_key,
            )
        except ApplicationError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=_error_payload(exc, request_id),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse(
                status_code=400,
                content=_error_payload(
                    InvalidRequestError(safe_exception_message(exc)), request_id
                ),
            )
        return JSONResponse(
            status_code=201,
            content={
                "artifact_id": record.artifact_id,
                "download_url": link.token,
                "filename": record.filename,
                "content_type": record.content_type,
                "size_bytes": record.size_bytes,
                "expires_at": record.expires_at,
                "request_id": request_id,
            },
        )

    async def download(
        self,
        artifact_id: str,
        token: str,
        *,
        request_id: str | None = None,
    ) -> FileResponse | JSONResponse:
        """Serve an artifact file if its signed link is authentic and valid."""
        request_id = request_id or new_request_id()
        try:
            signing_key = _signing_key(self._settings)
        except ApplicationError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=_error_payload(exc, request_id),
            )
        try:
            record = await self._store.retrieve(
                artifact_id, token, signing_key=signing_key
            )
        except ApplicationError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=_error_payload(exc, request_id),
            )
        if record is None:
            # Distinguish expired (410) from unknown/tampered (404): re-validate
            # the link so an authentic-but-expired token maps to 410 Gone.
            from free_claude_code.application.artifacts.links import validate_link

            check = validate_link(signing_key, token, artifact_id)
            if check.expired:
                return JSONResponse(
                    status_code=410,
                    content=_error_payload(
                        ApplicationUnavailableError("Artifact link has expired."),
                        request_id,
                    ),
                )
            return JSONResponse(
                status_code=404,
                content=_error_payload(
                    InvalidRequestError("Unknown artifact id."), request_id
                ),
            )
        return FileResponse(
            path=str(record.path),
            filename=record.filename,
            media_type=record.content_type,
            headers={"Content-Disposition": f'attachment; filename="{record.filename}"'},
        )


def _error_payload(exc: ApplicationError, request_id: str) -> JsonObject:
    return {
        "error": {
            "type": exc.kind.value if hasattr(exc.kind, "value") else str(exc.kind),
            "message": exc.message,
            "request_id": request_id,
        }
    }


__all__ = ["ArtifactsHandler"]