"""Cherry Studio provider metadata and config document overlays.

Cherry Studio is an independent Electron desktop AI client. It stores its
settings as a single JSON document (``config.json``) and exposes an
OpenAI-compatible API Gateway that other tools can call. FCC writes one entry
into Cherry Studio's ``customProviders`` list so that Cherry Studio routes new
sessions through FCC's proxy instead of its own built-in providers.
"""

from collections.abc import Mapping

from free_claude_code.application.model_catalog import (
    CatalogModel,
    context_window_for_client,
)
from free_claude_code.config.server_urls import proxy_v1_url
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.model_capabilities import ModelInputModality

CHERRY_PROVIDER_ID = "fcc-pro"
CHERRY_API_KEY_ENV = "FCC_CHERRY_STUDIO_API_KEY"
CHERRY_PROVIDER_NAME = "Free Claude Code"
CHERRY_CUSTOM_PROVIDERS_KEY = "customProviders"

# Cherry Studio identifies an OpenAI-compatible endpoint with this type value.
CHERRY_OPENAI_TYPE = "openai"


def build_cherry_provider(
    models: tuple[CatalogModel, ...],
    *,
    proxy_root_url: str,
    credential_ref: str,
) -> JsonObject:
    """Translate FCC's catalog into one Cherry Studio custom provider entry."""
    if not models:
        raise ValueError("Cherry Studio requires at least one routable FCC model")

    return {
        "id": CHERRY_PROVIDER_ID,
        "type": CHERRY_OPENAI_TYPE,
        "name": CHERRY_PROVIDER_NAME,
        "apiKeyEnv": credential_ref,
        "apiKey": credential_ref,
        "baseUrl": proxy_v1_url(proxy_root_url),
        "models": [_model_entry(model) for model in models],
        "enabled": True,
    }


def _model_entry(model: CatalogModel) -> JsonObject:
    entry: JsonObject = {"id": model.wire_slug, "name": model.display_name}
    if model.input_modalities is not None:
        entry["input"] = [
            modality.value
            for modality in ModelInputModality
            if modality in model.input_modalities
        ]
    entry["contextWindow"] = context_window_for_client(model)
    if model.max_output_tokens is not None:
        entry["maxTokens"] = model.max_output_tokens
    return entry


def same_provider(existing: Mapping[str, object], candidate: JsonObject) -> bool:
    """Return True when an existing entry already matches the candidate."""
    if not isinstance(existing, Mapping):
        return False
    return (
        existing.get("id") == candidate["id"]
        and existing.get("baseUrl") == candidate["baseUrl"]
        and existing.get("apiKey") == candidate["apiKey"]
        and _same_models(existing.get("models"), candidate["models"])
    )


def _same_models(
    existing: object, candidate: list[JsonObject]
) -> bool:
    if not isinstance(existing, list) or len(existing) != len(candidate):
        return False
    for left, right in zip(existing, candidate):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if left.get("id") != right.get("id") or left.get("name") != right.get("name"):
            return False
    return True