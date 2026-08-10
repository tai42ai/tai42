"""Interactions contract: the ``ask_user`` human-in-the-loop surface.

``models`` holds the durable request/state and validated response pydantic
models, the ``AnswerFormat`` enum, and the display-only ``MediaItem``/``MediaKind``
media models a question may carry; ``asker`` holds the ``AskUser`` callable
Protocol the engine-agnostic helper satisfies.
"""

from __future__ import annotations

from tai42_contract.interactions.asker import AskUser
from tai42_contract.interactions.models import (
    MEDIA_CAPTION_MAX_CHARS,
    MEDIA_DATA_URI_MAX_CHARS,
    MEDIA_MAX_ITEMS,
    MEDIA_TOTAL_URI_CHARS,
    MEDIA_URL_MAX_CHARS,
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
    InteractionState,
    MediaItem,
    MediaKind,
)

__all__ = [
    "MEDIA_CAPTION_MAX_CHARS",
    "MEDIA_DATA_URI_MAX_CHARS",
    "MEDIA_MAX_ITEMS",
    "MEDIA_TOTAL_URI_CHARS",
    "MEDIA_URL_MAX_CHARS",
    "AnswerFormat",
    "AskUser",
    "InteractionRequest",
    "InteractionResponse",
    "InteractionState",
    "MediaItem",
    "MediaKind",
]
