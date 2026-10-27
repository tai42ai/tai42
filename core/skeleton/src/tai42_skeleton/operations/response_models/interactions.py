"""Response models for the interactions operations: interaction actions, windows and pending interactions."""

from __future__ import annotations

from pydantic import BaseModel, JsonValue
from tai42_contract.interactions.models import AnswerFormat, MediaItem


class InteractionActionResult(BaseModel):
    """A single interaction terminal result: the id and its new ``status``.

    ``answered`` for the answer door, ``cancelled`` for the cancel door.
    """

    interaction_id: str
    status: str


class InteractionFrame(BaseModel):
    """One pending question's client add-frame (the paged list door and the live tail share it).

    ``format_payload`` is the verifier-stripped, otherwise-open payload (``null``
    when the question carries none). ``server_verified`` rides ONLY when a
    verifier was stripped; ``channel``/``recipient``/``origin``/``audience``/
    ``media`` ride only when the question set them (absent otherwise).
    """

    interaction_id: str
    group_id: str
    question: str
    answer_format: AnswerFormat
    format_payload: JsonValue
    created_at: str
    timeout_at: str
    sensitive: bool
    server_verified: bool | None = None
    channel: str | None = None
    recipient: str | None = None
    origin: str | None = None
    audience: str | None = None
    media: list[MediaItem] | None = None


class InteractionWindow(BaseModel):
    """One page of pending questions.

    ``truncated`` is always false (the pending index is the whole set, sliced in
    memory); ``next_page`` is ``null`` on the last page.
    """

    items: list[InteractionFrame]
    total: int
    page: int
    page_size: int
    next_page: int | None = None
    truncated: bool


class PendingInteraction(BaseModel):
    """One parked (async) ask on the audit surface.

    ``channel``/``recipient``/``audience``/``thread_id``/``expiry_at`` are
    nullable (a park may carry none); ``question`` is truncated to a preview.
    """

    interaction_id: str
    group_id: str
    question: str
    channel: str | None = None
    recipient: str | None = None
    audience: str | None = None
    thread_id: str | None = None
    expiry_at: str | None = None
    created_at: str
    mode: str


class PendingInteractionListing(BaseModel):
    """The parked-interactions audit body.

    The bounded (and, for a restricted caller, audience-filtered) slice plus its
    ``count``.
    """

    items: list[PendingInteraction]
    count: int
