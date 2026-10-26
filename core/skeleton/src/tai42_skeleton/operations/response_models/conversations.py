"""Response models for the conversations operations: routes, threads, transcripts, modes and target configs."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, RootModel
from tai42_contract.conversations import (
    ApiConversationRouteCreate,
    ChannelConversationRouteCreate,
    ConversationMode,
    ConversationTargetKind,
    TargetConversationConfig,
)

from tai42_skeleton.conversations.mode import ModeSource
from tai42_skeleton.conversations.models import AskReplyView, AskSendView, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.record_pages import TranscriptOrder


class ApiConversationRouteView(ApiConversationRouteCreate):
    """A stored ``api`` conversation route as every read serves it.

    The client-supplied fields plus the server-derived ``execution_key_fingerprint``, with
    ``callback_secret`` OMITTED — reads always strip it, so the view never advertises a field the wire
    withholds. The one-time ``callback_secret`` is returned separately by the create door.
    """

    execution_key_fingerprint: str


class ChannelConversationRouteView(ChannelConversationRouteCreate):
    """A stored ``channel`` conversation route as every read serves it, plus ``execution_key_fingerprint``."""

    execution_key_fingerprint: str


#: A stored conversation route as every read serves it, discriminated on ``door``.
ConversationRouteView = Annotated[ApiConversationRouteView | ChannelConversationRouteView, Field(discriminator="door")]


class ConversationRouteViewBody(RootModel[ConversationRouteView]):
    """One stored conversation route as a read serves it (the door's view variant)."""


class ConversationRouteListEnvelope(BaseModel):
    """A page of conversation routes (each secret-stripped) with the total count.

    ``unreadable`` counts the indexed routes whose row was gone or unparseable, so a shorter
    page is a truthful count and never a silent cut.
    """

    items: list[ConversationRouteView]
    total: int
    unreadable: int = 0


class ConversationRouteCreateResult(BaseModel):
    """The created/replaced route.

    ``callback_secret`` is the api-door callback signing secret surfaced ONCE (``null`` for a channel
    route); ``route`` is the stored view with its secret stripped.
    """

    created: bool
    route_name: str
    route: ConversationRouteView
    callback_secret: str | None


class ConversationRecordView(ConversationRecord):
    """A conversation answer record as a read door serves it.

    An admin read carries every
    field; the caller-scoped read withholds the route-key's internal detail — ``channel``,
    ``our_identity``, ``provider_message_id``, ``callback_url``, ``error``, ``attempts``, the
    per-part ``sends`` and all of ``ask`` but ``sensitive`` and of ``ask_reply`` but ``answer``
    are present only for an admin caller and are absent from the caller_view subset.
    """

    attempts: int | None = None
    ask: AskSendView | None = None
    ask_reply: AskReplyView | None = None


class ThreadSummaryRow(BaseModel):
    """One thread's activity summary, drawn from its newest readable record.

    ``last_activity_at`` is the route index's own sort score; ``last_delivery_status`` is
    that record's delivery-status wire string.
    """

    thread_id: str
    client_address: str
    last_activity_at: float
    message_count: int
    last_delivery_status: DeliveryStatus


class ThreadSummaryEnvelope(BaseModel):
    """A page of thread summaries.

    ``next_page`` is ``null`` on the last page; ``truncated`` is ``true`` when a filtered scan spent its
    budget before the page filled; ``unreadable`` counts the threads with no readable record, omitted
    from the page.
    """

    items: list[ThreadSummaryRow]
    total: int
    page: int
    page_size: int
    next_page: int | None
    truncated: bool
    unreadable: int = 0


class TranscriptEnvelope(BaseModel):
    """A page of a thread's records (admin full records or the caller_view subset).

    ``order`` is the direction served; ``next_page`` is ``null`` on the last page; ``unreadable`` counts
    the members whose row was gone or unparseable, omitted from the page; ``person_id`` names the linked
    person a person thread aggregates (``null`` for a route-keyed thread).
    """

    items: list[ConversationRecordView]
    total: int
    page: int
    page_size: int
    next_page: int | None
    order: TranscriptOrder
    truncated: bool
    unreadable: int = 0
    person_id: str | None


class MessageSearchEnvelope(BaseModel):
    """A page of a route's message-search matches (admin-only, so always full records).

    ``unreadable`` counts the scanned members whose row was gone or unparseable, omitted from the results.
    """

    items: list[ConversationRecordView]
    total: int
    page: int
    page_size: int
    next_page: int | None
    truncated: bool
    unreadable: int = 0


class FailedConversationsEnvelope(BaseModel):
    """Every answer record whose delivery ended ``failed`` (admin-only, full records).

    ``unreadable`` counts the indexed failed members whose row was gone or unparseable, omitted from
    the list.
    """

    items: list[ConversationRecordView]
    total: int
    unreadable: int = 0


class RouteRemoveResult(BaseModel):
    """A conversation route removal.

    ``removed`` says whether THIS call removed the routing row (``false`` when only an owed index
    reclamation was completed).
    """

    removed: bool
    route_name: str


class ThreadDeleteResult(BaseModel):
    """A thread forget. ``removed`` counts the answer records this call deleted.

    ``states_erased`` counts the thread-subject declared-state records this call forgot across the
    routes the thread spans (0 when the states feature is off or the thread held no declared state).
    """

    removed: int
    route_name: str
    thread_id: str
    states_erased: int


class PersonDeleteResult(BaseModel):
    """A person erase.

    ``removed`` counts the answer records deleted across the person's routes: in its aggregated thread, in
    the aggregated thread of every person it absorbed by a link, and in every route thread the erase
    cleared (one only the person and the persons it absorbed wrote in); ``erased`` says whether THIS call
    removed the person row. ``states_erased`` counts
    the declared-state records this call forgot — the person's own (``person``-subject) and its aggregated
    thread's (``thread``-subject) memory, the same for every absorbed person, and the ``thread``-subject
    memory of each route thread the erase cleared. ``absorbed_person_ids`` lists, sorted, the ids of the
    persons it absorbed that this call cleared (``[]`` when it absorbed none, or when its row was already
    gone): run traces keyed by those ids are the operator's to delete, and nothing else names them once the
    erase has finished.
    """

    person_id: str
    removed: int
    erased: bool
    states_erased: int
    absorbed_person_ids: list[str]


class ThreadMessageAck(BaseModel):
    """The stored id of an operator message sent by hand into a thread."""

    message_id: str
    thread_id: str


class ThreadModeView(BaseModel):
    """A thread's control mode and where it comes from.

    ``source`` is ``thread`` for a per-thread override or ``route`` for the route default.
    """

    mode: ConversationMode
    source: ModeSource


class ThreadModeSetResult(BaseModel):
    """A per-thread mode override write; ``source`` is always ``thread``."""

    route_name: str
    thread_id: str
    mode: ConversationMode
    source: ModeSource


class ConversationConfigListEnvelope(BaseModel):
    """A page of per-target conversation configs with the total count.

    ``unreadable`` counts the indexed configs whose row was gone or unparseable, so a shorter page is a
    truthful count and never a silent cut.
    """

    items: list[TargetConversationConfig]
    total: int
    unreadable: int = 0


class ConversationConfigSetResult(BaseModel):
    """The created/replaced per-target conversation config."""

    created: bool
    target_kind: ConversationTargetKind
    target_name: str
    config: TargetConversationConfig


class ConversationConfigDeleteResult(BaseModel):
    """A per-target conversation config removal."""

    removed: bool
    target_kind: ConversationTargetKind
    target_name: str
