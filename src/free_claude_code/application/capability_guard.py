"""Capability-aware routing guards.

These guards stop unsupported media from being silently forwarded to a provider
model that cannot consume it. They are strictly evidence-based: a request is only
rejected when the target model *positively* declares a modality set that excludes
the requested media. An unknown capability (a catalog lookup miss, or
``input_modalities is None``) is treated as unknown and is never blocked, so the
guards preserve the existing behavior for every catalog that does not advertise
modalities.
"""

from typing import Any

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.application.ports import ModelInfoLookup
from free_claude_code.core.anthropic import MessagesRequest
from free_claude_code.core.anthropic.content import get_block_attr, get_block_type
from free_claude_code.core.model_capabilities import ModelInputModality


def _content_blocks(message: Any) -> list[Any]:
    content = getattr(message, "content", None)
    return list(content) if isinstance(content, list) else []


def request_has_image_input(request: MessagesRequest) -> bool:
    """Return True when any message carries an image content block.

    Scans top-level ``image`` blocks and images nested inside ``tool_result``
    blocks, matching the image accounting already performed for token estimation
    (``core/anthropic/tokens.py``).
    """
    for message in request.messages:
        for block in _content_blocks(message):
            block_type = get_block_type(block)
            if block_type == "image":
                return True
            if block_type == "tool_result":
                nested = get_block_attr(block, "content")
                if isinstance(nested, list) and any(
                    get_block_type(part) == "image" for part in nested
                ):
                    return True
    return False


def guard_image_input_capability(
    request: MessagesRequest,
    provider_id: str,
    provider_model: str,
    model_info_lookup: ModelInfoLookup | None,
) -> None:
    """Reject an image-input request whose target model excludes image input.

    Raises ``InvalidRequestError`` (a clean 400, re-raised by the handler as an
    ``ApplicationError``) only when the target model *positively* declares an
    input-modality set that does not include IMAGE. A lookup miss or an
    ``input_modalities`` of ``None`` means the capability is unknown and the
    request is allowed through, preserving current behavior.
    """
    if not request_has_image_input(request):
        return
    if model_info_lookup is None:
        return
    info: ProviderModelInfo | None = model_info_lookup(provider_id, provider_model)
    if info is None or info.input_modalities is None:
        return
    if ModelInputModality.IMAGE not in info.input_modalities:
        declared = sorted(modality.value for modality in info.input_modalities)
        raise InvalidRequestError(
            f"model '{provider_id}/{provider_model}' does not accept image input; "
            f"its declared input modalities are {declared}"
        )