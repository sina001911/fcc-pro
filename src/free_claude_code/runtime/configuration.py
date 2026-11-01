"""Worker boundary for managed configuration; runtime state stays on the loop."""

from collections.abc import Mapping
from secrets import token_bytes

from anyio import CapacityLimiter, to_thread

from free_claude_code.config.admin.custom_providers import CustomProviderMutation
from free_claude_code.config.admin.persistence import (
    PreparedAdminUpdate,
    prepare_admin_update,
)
from free_claude_code.config.admin.state import ConfigInputValue, ValueState
from free_claude_code.config.admin.values import load_config_response, load_value_state
from free_claude_code.config.loader import ManagedConfigStore
from free_claude_code.config.settings import Settings
from free_claude_code.core.json_types import JsonObject


class ConfigurationService:
    def __init__(self, store: ManagedConfigStore) -> None:
        self._store = store
        self._revision_key = token_bytes(32)
        # Storage is serialized; its waiters must not exhaust FastAPI's worker pool.
        self._worker_limiter = CapacityLimiter(1)

    async def initialize(self) -> None:
        await to_thread.run_sync(self._store.initialize, limiter=self._worker_limiter)

    async def admin_config(self) -> JsonObject:
        snapshot = await to_thread.run_sync(
            self._store.read, limiter=self._worker_limiter
        )
        return load_config_response(snapshot, revision_key=self._revision_key)

    async def admin_values(self) -> ValueState:
        snapshot = await to_thread.run_sync(
            self._store.read, limiter=self._worker_limiter
        )
        return load_value_state(snapshot)

    async def prepare(
        self,
        updates: Mapping[str, ConfigInputValue],
        active_settings: Settings,
        custom_provider: CustomProviderMutation | None = None,
    ) -> PreparedAdminUpdate:
        snapshot = await to_thread.run_sync(
            self._store.read, limiter=self._worker_limiter
        )
        return prepare_admin_update(
            updates,
            snapshot,
            active_settings,
            custom_provider,
            revision_key=self._revision_key,
        )

    async def commit(self, prepared: PreparedAdminUpdate) -> JsonObject:
        if not prepared.valid:
            raise ValueError("Cannot commit an invalid Admin update")
        await to_thread.run_sync(
            self._store.commit,
            prepared.target_values,
            prepared.expected,
            limiter=self._worker_limiter,
        )
        return prepared.applied_response()

    async def media_model_status(self) -> JsonObject:
        """Return raw media-model settings for admin UI consumption."""
        snapshot = await to_thread.run_sync(
            self._store.read, limiter=self._worker_limiter
        )
        settings = snapshot.settings
        return {
            "image_generation_models": settings.image_generation_models,
            "video_generation_models": settings.video_generation_models,
            "image_generation_default": settings.image_generation_default,
            "video_generation_default": settings.video_generation_default,
        }

    async def update_media_models(
        self,
        image_models: Mapping[str, bool],
        video_models: Mapping[str, bool],
        image_default: str | None,
        video_default: str | None,
    ) -> JsonObject:
        """Persist admin-managed image/video model selections."""
        import json

        updates: dict[str, ConfigInputValue] = {}
        if image_models:
            updates["IMAGE_GENERATION_MODELS"] = json.dumps(dict(image_models))
        if video_models:
            updates["VIDEO_GENERATION_MODELS"] = json.dumps(dict(video_models))
        if image_default is not None:
            updates["IMAGE_GENERATION_DEFAULT"] = image_default
        if video_default is not None:
            updates["VIDEO_GENERATION_DEFAULT"] = video_default

        if not updates:
            return {"applied": True, "valid": True, "errors": []}

        snapshot = await to_thread.run_sync(
            self._store.read, limiter=self._worker_limiter
        )
        prepared = prepare_admin_update(
            updates,
            snapshot,
            snapshot.settings,
            revision_key=self._revision_key,
        )
        if not prepared.valid:
            return prepared.validation_response() | {
                "applied": False,
                "valid": False,
                "errors": list(prepared.errors),
                "pending_fields": list(prepared.pending_fields),
            }

        await to_thread.run_sync(
            self._store.commit,
            prepared.target_values,
            prepared.expected,
            limiter=self._worker_limiter,
        )
        return prepared.applied_response() | {
            "applied": True,
            "valid": True,
            "errors": [],
        }
