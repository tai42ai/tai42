"""Interactions contract: the ``ask_user`` human-in-the-loop surface.

``models`` holds the durable request/state and validated response pydantic
models, the ``SuspendedInteraction`` sentinel an async ask returns, the
``AnswerFormat`` enum, and the display-only ``MediaItem``/``MediaKind`` media
models a question may carry; ``asker`` holds the ``AskUser`` callable Protocol
the engine-agnostic helper satisfies and the ``check_ask_timing`` guard;
``continuation`` holds the generic driver-continuation context an async ask reads.
"""

from __future__ import annotations

from tai42_contract.interactions.asker import AskUser, check_ask_timing
from tai42_contract.interactions.continuation import (
    EXPIRY_ANSWER,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    SUSPENDED_INTERACTION_MARKER_KEY,
    get_park_completion,
    get_resume_continuation_tool,
    read_suspended_interaction_marker,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)
from tai42_contract.interactions.models import (
    MEDIA_CAPTION_MAX_CHARS,
    MEDIA_DATA_URI_MAX_CHARS,
    MEDIA_MAX_ITEMS,
    MEDIA_ROUTE_PREFIX,
    MEDIA_TOTAL_URI_CHARS,
    MEDIA_URL_MAX_CHARS,
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
    InteractionState,
    MediaItem,
    MediaKind,
    SuspendedInteraction,
    check_media_list,
)

__all__ = [
    "EXPIRY_ANSWER",
    "MEDIA_CAPTION_MAX_CHARS",
    "MEDIA_DATA_URI_MAX_CHARS",
    "MEDIA_MAX_ITEMS",
    "MEDIA_ROUTE_PREFIX",
    "MEDIA_TOTAL_URI_CHARS",
    "MEDIA_URL_MAX_CHARS",
    "PARK_COMPLETION_FAILED",
    "PARK_COMPLETION_SUCCEEDED",
    "SUSPENDED_INTERACTION_MARKER_KEY",
    "AnswerFormat",
    "AskUser",
    "InteractionRequest",
    "InteractionResponse",
    "InteractionState",
    "MediaItem",
    "MediaKind",
    "SuspendedInteraction",
    "check_ask_timing",
    "check_media_list",
    "get_park_completion",
    "get_resume_continuation_tool",
    "read_suspended_interaction_marker",
    "reset_park_completion",
    "reset_resume_continuation_tool",
    "set_park_completion",
    "set_resume_continuation_tool",
    "suspended_interaction_marker",
]
