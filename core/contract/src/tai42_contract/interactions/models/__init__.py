"""Pydantic v2 models for the ``ask`` interactions capability.

``InteractionRequest`` is the durable question written to a per-group stream;
``InteractionResponse`` is the validated answer pushed onto the reply channel;
``InteractionState`` is the mutable record the answer endpoint reads to guard
against a duplicate answer and to validate the submitted value;
``SuspendedInteraction`` is the sentinel an ``async`` ask returns in place of an
answer when it parks the caller. The answer-format enums, the display media and
location models, and the per-send form models sit beside them.
"""

from __future__ import annotations

from tai42_contract.interactions.models.formats import AnswerFormat, AnswerMismatchPolicy
from tai42_contract.interactions.models.forms import (
    FormData,
    FormOption,
    FormPage,
    check_form_data,
    check_form_pages,
)
from tai42_contract.interactions.models.location import (
    LOCATION_ADDRESS_MAX_CHARS,
    LOCATION_NAME_MAX_CHARS,
    LocationElement,
)
from tai42_contract.interactions.models.media import (
    FILE_MEDIA_KINDS,
    LOCAL_HTTP_HOSTS,
    MEDIA_CAPTION_MAX_CHARS,
    MEDIA_DATA_URI_MAX_CHARS,
    MEDIA_FILENAME_MAX_CHARS,
    MEDIA_MAX_ITEMS,
    MEDIA_ROUTE_PREFIX,
    MEDIA_TOTAL_URI_CHARS,
    MEDIA_URL_MAX_CHARS,
    MediaItem,
    MediaKind,
    check_media_list,
    served_media_id,
    validate_action_url,
)
from tai42_contract.interactions.models.request import (
    MISMATCH_NOTICE_MAX_CHARS,
    QUESTION_MAX_CHARS,
    InteractionRequest,
    check_addressing,
)
from tai42_contract.interactions.models.response import (
    InteractionResponse,
    InteractionState,
    ResumeBuffered,
    SuspendedInteraction,
)

__all__ = [
    "FILE_MEDIA_KINDS",
    "LOCAL_HTTP_HOSTS",
    "LOCATION_ADDRESS_MAX_CHARS",
    "LOCATION_NAME_MAX_CHARS",
    "MEDIA_CAPTION_MAX_CHARS",
    "MEDIA_DATA_URI_MAX_CHARS",
    "MEDIA_FILENAME_MAX_CHARS",
    "MEDIA_MAX_ITEMS",
    "MEDIA_ROUTE_PREFIX",
    "MEDIA_TOTAL_URI_CHARS",
    "MEDIA_URL_MAX_CHARS",
    "MISMATCH_NOTICE_MAX_CHARS",
    "QUESTION_MAX_CHARS",
    "AnswerFormat",
    "AnswerMismatchPolicy",
    "FormData",
    "FormOption",
    "FormPage",
    "InteractionRequest",
    "InteractionResponse",
    "InteractionState",
    "LocationElement",
    "MediaItem",
    "MediaKind",
    "ResumeBuffered",
    "SuspendedInteraction",
    "check_addressing",
    "check_form_data",
    "check_form_pages",
    "check_media_list",
    "served_media_id",
    "validate_action_url",
]
