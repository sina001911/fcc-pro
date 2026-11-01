"""Capability resolution for image and video generation models.

Determines whether a specific model is usable for image or video generation
by combining three sources of evidence:

1. Provider descriptor flags (``image_generation`` / ``video_generation``)
   — the coarse-grained capability declared in ``config/provider_catalog.py``.
2. Model-level output modalities from discovery — e.g. a model whose
   ``output_modalities`` includes ``IMAGE`` or ``VIDEO`` is verified.
3. Admin overrides persisted through the FCC settings system — the
   ``IMAGE_GENERATION_MODELS`` / ``VIDEO_GENERATION_MODELS`` JSON maps let an
   administrator explicitly enable or disable individual models.

A model is only considered usable when at least one of these sources confirms
it. Provider-level support alone does not imply every model from that provider
is capable; unknown models are reported as ``"Unknown"`` and are not surfaced
as verified.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.model_capabilities import ModelOutputModality


def parse_media_models(value: str) -> dict[str, bool]:
    """Parse a JSON-encoded ``{"provider/model": bool, ...}`` selection map.

    Returns an empty dict for blank, missing, or malformed input so callers
    can safely use ``.get(ref, False)`` semantics.
    """
    if not value or not value.strip():
        return {}
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(result, Mapping):
        return {}
    parsed: dict[str, bool] = {}
    for key, flag in result.items():
        if isinstance(key, str) and isinstance(flag, bool):
            parsed[key] = flag
    return parsed


def is_image_generation_provider(provider_id: str) -> bool:
    """Return ``True`` when the provider descriptor declares image generation."""
    descriptor = PROVIDER_CATALOG.get(provider_id)
    return descriptor is not None and descriptor.image_generation


def is_video_generation_provider(provider_id: str) -> bool:
    """Return ``True`` when the provider descriptor declares video generation."""
    descriptor = PROVIDER_CATALOG.get(provider_id)
    return descriptor is not None and descriptor.video_generation


def is_model_enabled_for_image(
    provider_model_ref: str, settings: Settings
) -> bool:
    """Return ``True`` when the model is explicitly enabled for image generation."""
    return parse_media_models(settings.image_generation_models).get(
        provider_model_ref, False
    )


def is_model_enabled_for_video(
    provider_model_ref: str, settings: Settings
) -> bool:
    """Return ``True`` when the model is explicitly enabled for video generation."""
    return parse_media_models(settings.video_generation_models).get(
        provider_model_ref, False
    )


def image_generation_model_status(
    provider_model_ref: str,
    settings: Settings,
    output_modalities: frozenset[ModelOutputModality] | None = None,
) -> str:
    """Return one of ``"Verified"``, ``"Unsupported"``, or ``"Unknown"``.

    * ``"Verified"`` — the model is usable for image generation. Either the
      provider descriptor declares image generation and the model's discovered
      output modalities include ``IMAGE``, or the administrator has explicitly
      enabled this model.
    * ``"Unsupported"`` — the provider does not declare image generation, so
      no model from that provider can be used.
    * ``"Unknown"`` — the provider declares image generation but there is no
      model-level evidence and no admin override yet. The model is *not*
      surfaced as verified.
    """
    provider_id = provider_model_ref.split("/", 1)[0]
    if not is_image_generation_provider(provider_id):
        return "Unsupported"
    if output_modalities is not None and ModelOutputModality.IMAGE in output_modalities:
        return "Verified"
    if is_model_enabled_for_image(provider_model_ref, settings):
        return "Verified"
    return "Unknown"


def video_generation_model_status(
    provider_model_ref: str,
    settings: Settings,
    output_modalities: frozenset[ModelOutputModality] | None = None,
) -> str:
    """Return one of ``"Verified"``, ``"Unsupported"``, or ``"Unknown"``.

    See :func:`image_generation_model_status` for the meaning of each status.
    """
    provider_id = provider_model_ref.split("/", 1)[0]
    if not is_video_generation_provider(provider_id):
        return "Unsupported"
    if output_modalities is not None and ModelOutputModality.VIDEO in output_modalities:
        return "Verified"
    if is_model_enabled_for_video(provider_model_ref, settings):
        return "Verified"
    return "Unknown"


def is_image_generation_model(
    provider_model_ref: str,
    settings: Settings,
    output_modalities: frozenset[ModelOutputModality] | None = None,
) -> bool:
    """Return ``True`` when the model is usable for image generation."""
    return (
        image_generation_model_status(
            provider_model_ref, settings, output_modalities
        )
        == "Verified"
    )


def is_video_generation_model(
    provider_model_ref: str,
    settings: Settings,
    output_modalities: frozenset[ModelOutputModality] | None = None,
) -> bool:
    """Return ``True`` when the model is usable for video generation."""
    return (
        video_generation_model_status(
            provider_model_ref, settings, output_modalities
        )
        == "Verified"
    )


def resolve_default_image_model(settings: Settings) -> str | None:
    """Return the configured default image-generation model, if any."""
    value = settings.image_generation_default
    if not value or not value.strip():
        return None
    return value.strip()


def resolve_default_video_model(settings: Settings) -> str | None:
    """Return the configured default video-generation model, if any."""
    value = settings.video_generation_default
    if not value or not value.strip():
        return None
    return value.strip()


__all__ = [
    "parse_media_models",
    "is_image_generation_provider",
    "is_video_generation_provider",
    "is_model_enabled_for_image",
    "is_model_enabled_for_video",
    "image_generation_model_status",
    "video_generation_model_status",
    "is_image_generation_model",
    "is_video_generation_model",
    "resolve_default_image_model",
    "resolve_default_video_model",
]