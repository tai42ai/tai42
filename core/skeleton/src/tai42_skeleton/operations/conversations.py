"""Conversation-route management operations — the routing-table surface behind the
``/api/conversations*`` doors, the ``tai conversations`` CLI and the MCP tools.

A route binds an inbound door (``api`` or ``channel``) to a target — an ``agent`` run or a
``tool`` dispatch — and the ``execution_key`` that turn runs AS. A row's ``callback_secret``
is shown ONCE at create and withheld from every read. ``create_conversation_route``
DELEGATES authority, so it carries the ``authority_changing`` tier and binds the key BEFORE
any write.

Every routing operation requires the redis conversations backend and otherwise refuses
with a loud 501 (``NotSupportedError``).
"""

from __future__ import annotations

import secrets
from typing import Any, Literal, get_args
from urllib.parse import quote

from pydantic import BaseModel, Field
from tai42_contract.conversations import ROUTE_NAME_RE, ConversationRoute, ConversationRouteCreate
from tai42_kit.utils.data import get_compiled_jq

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefused,
)
from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations._authority import (
    Caller,
    assert_execution_key_bindable,
    require_admin,
    resolve_caller,
)
from tai42_skeleton.operations.errors import ForbiddenError, NotSupportedError

# Surfaced before a create does any bind work it would then have to discard.
_NO_BACKEND = "conversation routes require the redis conversations backend"


def _require_backend() -> BaseConversationsManager:
    manager = get_conversations_manager()
    if isinstance(manager, InMemoryConversationsManager):
        raise NotSupportedError(_NO_BACKEND)
    return manager


def _public_route_view(route: ConversationRoute) -> dict[str, Any]:
    """A stored row for a read response, its ``callback_secret`` stripped."""
    data = route.model_dump(mode="json")
    data.pop("callback_secret", None)
    return data


def _validate_route_name(route_name: str) -> None:
    if not ROUTE_NAME_RE.fullmatch(route_name):
        raise BadRequestError(f"route_name must be a slug matching {ROUTE_NAME_RE.pattern!r}: {route_name!r}")


async def _assert_target_exists(create: ConversationRouteCreate) -> None:
    """Existence only: the turn runs as the key, so the key's authority over the target is
    deliberately not checked here. An ``agent`` target must be registered; a ``tool`` target
    must resolve on the live tool registry. A miss is a loud 404, mirroring either side."""
    from tai42_skeleton.app import instance
    from tai42_skeleton.tools.binding import UnknownToolError

    if create.target_kind == "agent":
        if create.target_name not in instance.app.agents.all_agents():
            raise NotFoundError(f"agent not found: {create.target_name!r}")
        return
    try:
        await instance.app.tools.get_tool(create.target_name)
    except UnknownToolError as exc:
        raise NotFoundError(f"tool not found: {create.target_name!r}") from exc


def _assert_exprs_compile(create: ConversationRouteCreate) -> None:
    """Compile a tool target's jq programs at create so an invalid one is refused here, not
    at the first message. The model already forbids exprs on an ``agent`` target."""
    for field, expr in (("payload_expr", create.payload_expr), ("reply_expr", create.reply_expr)):
        if expr is None:
            continue
        try:
            get_compiled_jq(expr)
        except Exception as exc:
            raise BadRequestError(f"invalid {field}: {exc}") from exc


def _door_flip_refusal(refused: DoorFlipRefused) -> BadRequestError:
    """The operator-facing refusal for an edit that would change the ``door`` of a route
    that already HOLDS threads.

    The two doors key their threads differently — an api thread names its owning caller
    principal in its own id, a channel thread names the medium's address — and the read
    doors authorize from that shape plus the route's current ``door``. Flipping it therefore
    404s the owner out of a transcript they own, and hands the single-message door a
    different answer from the transcript door about the same thread. The threads cannot be
    re-keyed, so the flip is refused rather than half-applied — and refused by the write
    itself, so a first message opening a thread cannot slip in behind a separate check."""
    return BadRequestError(
        f"conversation route {refused.route_name!r} holds {refused.held} thread(s) opened on its "
        f"{refused.from_door!r} door and cannot be changed to {refused.to_door!r}: delete the route "
        "(which reclaims its threads) and create it again under the new door, or create the new door "
        "under a different route_name"
    )


async def _unclaimed_channel_identity(
    manager: BaseConversationsManager, *, route_name: str, channel: str, our_identity: str
) -> str:
    """The canonical ``our_identity`` a ``channel`` row is STORED under, refused when
    another route already claims that ``(channel, identity)`` pair. Inbound routing matches
    the canonical form, so a second claimant would make every message to that identity
    unresolvable; it is refused here at the write instead.
    """
    try:
        identity = canonical_address(our_identity)
    except ValueError as exc:
        raise BadRequestError(f"invalid our_identity: {exc}") from exc
    for row in (await manager.list_routes()).values():
        if (
            row.route_name != route_name
            and row.door == "channel"
            and row.channel == channel
            and row.our_identity is not None
            and canonical_address(row.our_identity) == identity
        ):
            raise BadRequestError(f"channel {channel!r} identity {identity!r} is already routed by {row.route_name!r}")
    return identity


@operation(summary="List conversation routes", tags=["conversations"], errors=[NotSupportedError])
async def list_conversation_routes() -> dict[str, Any]:
    """Every stored conversation route, each with its ``callback_secret`` withheld.
    Returns ``{"items", "total"}``.
    """
    manager = _require_backend()
    routes = await manager.list_routes()
    items = [_public_route_view(route) for route in routes.values()]
    return {"items": items, "total": len(items)}


@operation(
    summary="Get a conversation route",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def get_conversation_route(route_name: str) -> dict[str, Any]:
    """One conversation route by name, with its ``callback_secret`` withheld. An
    unknown name is a loud 404; a name that is not a valid slug is a 400."""
    _validate_route_name(route_name)
    manager = _require_backend()
    route = await manager.get_route(route_name)
    if route is None:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    return _public_route_view(route)


@operation(
    summary="Create or replace a conversation route",
    tags=["conversations"],
    destructive=True,
    authority_changing=True,
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=ConversationRouteCreate,
)
async def create_conversation_route(
    route_name: str,
    door: str,
    target_kind: str,
    target_name: str,
    execution_key: str,
    payload_expr: str | None = None,
    reply_expr: str | None = None,
    channel: str | None = None,
    our_identity: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """Create a conversation route from its flat parameters — an UPSERT, so this is the
    create path AND the edit path for a route of that name.

    ``execution_key`` is the api-key identity the turn runs AS; the caller must be allowed
    to delegate it and it must be usable by a tokenless fire, both decided BEFORE the write
    so a refusal leaves any existing row untouched. ``target_name`` must merely EXIST — the
    agent (``target_kind=agent``) or tool (``target_kind=tool``) — the key's live grants
    bound the turn at fire. A tool target's ``payload_expr``/``reply_expr`` jq programs, when
    given, are compiled here so an invalid one is refused at create, not at first message. A
    ``channel`` row's ``our_identity`` is stored canonicalized and must not already be routed
    on that channel. An edit that would change the ``door`` of a route already HOLDING
    threads is refused: the two doors key their threads differently and the read doors
    authorize from that shape, so the flip would lock owners out of their own transcripts.
    An ``api`` row's ``callback_secret`` is minted here and returned ONCE.
    Returns ``{"created", "route_name", "route", "callback_secret"}``.
    """
    # Validate the whole body shape at the operation, not the edge: the MCP tool and a
    # direct call take these flat parameters and bypass the HTTP extractor.
    try:
        create = ConversationRouteCreate(
            route_name=route_name,
            door=door,  # pyright: ignore[reportArgumentType]
            target_kind=target_kind,  # pyright: ignore[reportArgumentType]
            target_name=target_name,
            payload_expr=payload_expr,
            reply_expr=reply_expr,
            execution_key=execution_key,
            channel=channel,
            our_identity=our_identity,
            callback_url=callback_url,
        )
    except ValueError as exc:
        raise BadRequestError(f"invalid conversation route: {exc}") from exc

    manager = _require_backend()

    await _assert_target_exists(create)
    _assert_exprs_compile(create)

    stored = create.model_dump()
    # Only a ``channel`` row carries both fields; its identity is stored canonicalized.
    if create.channel is not None and create.our_identity is not None:
        stored["our_identity"] = await _unclaimed_channel_identity(
            manager, route_name=create.route_name, channel=create.channel, our_identity=create.our_identity
        )

    execution_key_fingerprint = await assert_execution_key_bindable(await resolve_caller(), create.execution_key)

    # Signs the api-door callback; a ``channel`` row signs nothing and carries no secret.
    callback_secret = secrets.token_urlsafe(32) if create.door == "api" else None

    route = ConversationRoute(
        **stored,
        callback_secret=callback_secret,
        execution_key_fingerprint=execution_key_fingerprint,
    )
    try:
        created = await manager.put_route(route)
    except DoorFlipRefused as refused:
        raise _door_flip_refusal(refused) from refused
    return {
        "created": created,
        "route_name": route.route_name,
        "route": _public_route_view(route),
        "callback_secret": callback_secret,
    }


@operation(
    summary="Read one conversation answer record",
    tags=["conversations"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
)
async def get_conversation_message(route_name: str, message_id: str) -> dict[str, Any]:
    """One conversation answer record by ``message_id`` under ``route_name``, caller-scoped.

    An api-door record is readable by the caller that invoked the turn or by an admin; a
    channel-door record is admin-only. A missing record or one on another route is a 404; a
    record that exists but is not the caller's is a 403. An admin reads the whole record;
    the caller reads the caller-safe projection, which withholds the internal detail of the
    route key's run.
    """
    _validate_route_name(route_name)
    _require_backend()
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    record = await ConversationRecordStore(ConversationsSettings()).get_record(message_id)
    if record is None or record.route_name != route_name:
        raise NotFoundError(f"conversation record not found: {message_id!r}")
    caller = await resolve_caller()
    if caller.is_admin:
        return record.view()
    if record.caller_principal != caller.caller_id:
        # A channel record's ``None`` never equals a real caller id, so this refuses it too.
        raise ForbiddenError("you may only read conversation records from turns you invoked")
    return record.caller_view()


#: The largest page either thread read door serves. A larger ``page_size`` is capped to it —
#: valid data, not an error — so one read can never ask for an unbounded slice.
MAX_THREAD_PAGE_SIZE = 200

#: The highest page number either thread read door serves. A page past it names a rank the
#: index cannot be sliced at, which the backend answers with an error of its own; it is a
#: malformed window and is refused as one here.
MAX_THREAD_PAGE = 1_000_000

#: The transcript orders the door serves: ``asc`` is the transcript order (oldest first),
#: ``desc`` the live-tail order, where page 1 always holds the newest messages.
TranscriptOrder = Literal["asc", "desc"]
TRANSCRIPT_ORDERS = get_args(TranscriptOrder)


class ThreadWindowQuery(BaseModel):
    """The ``?page=``/``?pageSize=`` window the route thread listing takes.

    Spec metadata only: a GET carries no body and the door parses its own query at the HTTP
    edge, so this model is what the emitted OpenAPI publishes as the door's ``in: query``
    parameters — and therefore what a generated client sends. Its field names are the QUERY
    keys, not the operation's Python parameter names."""

    page: int = Field(default=1, ge=1, description="1-based page number, newest activity first.")
    page_size: int = Field(
        default=50,
        ge=1,
        alias="pageSize",
        description=f"Items per page. A larger value is capped to {MAX_THREAD_PAGE_SIZE}, never refused.",
    )


class TranscriptQuery(ThreadWindowQuery):
    """The transcript door's query on top of the shared window. ``thread_id`` is REQUIRED —
    a client generated without it calls the door with no thread to read and is answered
    400."""

    page: int = Field(default=1, ge=1, description="1-based page number, in the requested ``order``.")
    thread_id: str = Field(description="The thread to read, as the send door returned it.")
    order: TranscriptOrder = Field(
        default="asc",
        description="``asc`` reads the transcript oldest first; ``desc`` is the live-tail order.",
    )


def _page_bounds(page: int, page_size: int) -> tuple[int, int]:
    """The ``(offset, limit)`` a page/pageSize pair names. Both must be at least 1 and
    ``page`` at most :data:`MAX_THREAD_PAGE`; a page size above the cap is capped, never
    refused."""
    if page < 1 or page_size < 1:
        raise BadRequestError(f"page and page_size must be >= 1, got page={page} page_size={page_size}")
    if page > MAX_THREAD_PAGE:
        raise BadRequestError(f"page must be <= {MAX_THREAD_PAGE}, got page={page}")
    limit = min(page_size, MAX_THREAD_PAGE_SIZE)
    return (page - 1) * limit, limit


def _next_page(page: int, limit: int, total: int) -> int | None:
    """The next page number, or ``None`` on the last page. Read from the INDEXED total, not
    the returned count: a page shortened by rows that expired under it is not the end."""
    return page + 1 if page * limit < total else None


async def _require_route(manager: BaseConversationsManager, route_name: str) -> ConversationRoute:
    """The route a thread read is against. Refuses a read on a route that does not exist, so
    an unknown route is a loud 404 and not an empty listing. Called only once the reader is
    authorized: existence is a fact the answer discloses."""
    route = await manager.get_route(route_name)
    if route is None:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    return route


def _thread_not_found(thread_id: str) -> NotFoundError:
    """The ONE answer the transcript door gives for every thread the caller may not read —
    absent, expired, another principal's, or a channel thread. A caller that could tell
    those apart could probe whether a given address has ever talked to a given route."""
    return NotFoundError(f"conversation thread not found: {thread_id!r}")


def _caller_owns_thread(route: ConversationRoute, thread_id: str, caller: Caller) -> bool:
    """Whether ``caller`` owns ``thread_id`` — decided from the thread's IDENTITY alone, so
    it is decided before any record is read and cannot vary with the page asked for.

    A bridge thread id is ``bridge:{route_name}:{client_address}``, and on the api door the
    address is ``{percent-encoded caller principal}/{external user id}``. So an api-door
    thread names its owner in its own id, and the caller's own principal encoded the same
    way is the only prefix that can match. A ``channel`` route's addresses are the medium's,
    attested by the provider and owned by nobody who can call this door, so its threads are
    admin-only by construction — the route's door is checked too, and not just the address
    shape, so a channel address spelled like an api one still cannot be claimed."""
    if route.door != "api" or caller.caller_id is None:
        return False
    prefix = f"{BRIDGE_THREAD_PREFIX}{route.route_name}:"
    if not thread_id.startswith(prefix):
        return False
    return thread_id[len(prefix) :].startswith(f"{quote(caller.caller_id, safe='')}/")


@operation(
    summary="List a conversation route's threads",
    tags=["conversations"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=ThreadWindowQuery,
)
async def list_conversation_threads(route_name: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """The threads of ``route_name``, newest activity first, one page at a time.

    Admin-only — a thread listing spans every caller and address on the route, so it is not
    caller-scoped. Each item carries ``thread_id``, ``client_address``, ``message_count``
    and ``last_delivery_status`` summarized from the thread's newest readable record, plus
    ``last_activity_at`` — the route index's own score, which is what this listing SORTS by,
    so the moment shown and the position it is shown in always agree. That score is stamped
    when a record is created and again when its turn completes; a later delivery transition
    on the same record moves the record's ``updated_at`` but not the thread's activity.

    Authorization is decided BEFORE the route is looked up, so a non-admin is refused the
    same way whether the name routes or not — a 404-here/403-there pair would answer which
    route names exist to a caller with no business knowing. An unknown route is a loud 404
    to an admin; a ``page`` or ``page_size`` below 1, or a ``page`` above the served
    maximum, is a 400. Returns ``{"items", "total", "page", "page_size", "next_page"}``,
    where ``total`` counts the route's indexed threads.
    """
    _validate_route_name(route_name)
    manager = _require_backend()
    require_admin(await resolve_caller())
    await _require_route(manager, route_name)
    offset, limit = _page_bounds(page, page_size)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    listed = await ConversationRecordStore(ConversationsSettings()).list_route_threads(
        route_name, offset=offset, limit=limit
    )
    items = [
        {
            "thread_id": thread.thread_id,
            "client_address": thread.client_address,
            "last_activity_at": thread.last_activity_at,
            "message_count": thread.message_count,
            "last_delivery_status": thread.last_delivery_status.value,
        }
        for thread in listed.threads
    ]
    return {
        "items": items,
        "total": listed.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, listed.total),
    }


@operation(
    summary="Read a conversation thread's transcript",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=TranscriptQuery,
)
async def get_conversation_thread(
    route_name: str, thread_id: str, page: int = 1, page_size: int = 50, order: str = "asc"
) -> dict[str, Any]:
    """One thread's records under ``route_name``, one page at a time — caller-scoped.

    ``order`` picks the direction: ``asc`` (the default) reads the transcript oldest first,
    ``desc`` reads it newest first, which is the order a live tail wants because page 1 then
    always holds the latest messages. ``page``/``page_size`` window that order from its own
    end, so page 1 of ``desc`` is the newest page and never the oldest.

    The reader is authorized from the thread's IDENTITY, BEFORE any record is read, so no
    page can disclose what page 1 would not: an admin reads whole records of any thread, and
    a non-admin reads the caller-safe projection of an api-door thread its own principal
    keys. Everything else — an unknown thread, a thread the index no longer holds, another
    principal's, any channel thread, and a ``route_name`` that does not route at all —
    answers ONE uniform 404 to a non-admin, so the door cannot be used to probe whether an
    address has ever talked to a route, or which route names exist. An unknown
    ``route_name`` is its own 404 only to an admin. A ``page`` or ``page_size`` below 1, a
    ``page`` above the served maximum, a blank ``thread_id`` or an unknown ``order`` is a
    400 — the caller's own input, which discloses nothing.

    A thread the index still holds but whose rows have expired under the retention TTL is
    NOT that 404: it reads as an empty page carrying the indexed ``total``, until the prune
    pass reclaims the members and the thread becomes unknown.

    Returns ``{"items", "total", "page", "page_size", "next_page", "order"}``, where
    ``total`` counts the thread's indexed records.
    """
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    if order not in TRANSCRIPT_ORDERS:
        raise BadRequestError(f"order must be one of {list(TRANSCRIPT_ORDERS)}, got {order!r}")
    offset, limit = _page_bounds(page, page_size)
    manager = _require_backend()
    caller = await resolve_caller()
    if caller.is_admin:
        # An admin may know which names route, so the unknown route keeps its own 404.
        await _require_route(manager, route_name)
    else:
        # One answer for "no such route" and "not your thread": the reader is authorized
        # from the thread id alone, so the route's existence never reaches the answer.
        route = await manager.get_route(route_name)
        if route is None or not _caller_owns_thread(route, thread_id, caller):
            raise _thread_not_found(thread_id)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    transcript = await ConversationRecordStore(ConversationsSettings()).list_thread_records(
        route_name, thread_id, offset=offset, limit=limit, newest_first=order == "desc"
    )
    if transcript.total == 0:
        raise _thread_not_found(thread_id)
    view = (lambda record: record.view()) if caller.is_admin else (lambda record: record.caller_view())
    return {
        "items": [view(record) for record in transcript.records],
        "total": transcript.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, transcript.total),
        "order": order,
    }


@operation(
    summary="List failed conversation deliveries",
    tags=["conversations"],
    errors=[ForbiddenError, NotSupportedError],
)
async def list_failed_conversations() -> dict[str, Any]:
    """Every answer record whose delivery ended ``failed``. Admin-only — the listing spans
    every route and caller, so it is not caller-scoped. Returns ``{"items", "total"}``."""
    _require_backend()
    require_admin(await resolve_caller())
    from tai42_skeleton.conversations.models import DeliveryStatus
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    records = await ConversationRecordStore(ConversationsSettings()).list_by_status(frozenset({DeliveryStatus.FAILED}))
    items = [record.view() for record in records]
    return {"items": items, "total": len(items)}


@operation(
    summary="Delete a conversation route",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def delete_conversation_route(route_name: str) -> dict[str, Any]:
    """Delete a conversation route by name, along with the thread indexes it owned.

    Those indexes carry no TTL and the prune pass only walks LIVE routes, so a delete that
    left them behind would strand them unreachable forever; the answer records they name
    keep their own retention TTL and are not touched.

    The reclamation is RETRYABLE, because it can be interrupted (a socket timeout, a
    SIGTERM, a Redis blip) after the routing row is already gone: a name whose route index
    survives is one whose reclamation is still owed, so this door re-runs it and answers
    ``removed=false`` rather than the 404 that would leave those keys unnameable forever.
    Only a name that neither routes nor owes reclamation is the loud 404. A name that is not
    a valid slug is a 400. Returns ``{"removed", "route_name"}``, where ``removed`` says
    whether THIS call removed the routing row.
    """
    _validate_route_name(route_name)
    manager = _require_backend()
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    store = ConversationRecordStore(ConversationsSettings())
    removed = await manager.delete_route(route_name)
    if not removed and await store.count_route_threads(route_name) == 0:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    # After the routing row, never before: no further message can open a thread on a name
    # that no longer routes. A turn already IN FLIGHT still completes behind this, which is
    # why the create writes the thread indexes only while the row stands and the completion
    # write re-stamps the route index only while the thread's own index still holds
    # members — either one unguarded would re-create a pair nothing walks and no TTL expires.
    await store.drop_route_threads(route_name)
    return {"removed": removed, "route_name": route_name}
