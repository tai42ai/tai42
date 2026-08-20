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
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, Field
from tai42_contract.conversations import (
    CONVERSATION_MODES,
    ROUTE_NAME_RE,
    ConversationRoute,
    ConversationRouteCreate,
    ConversationTargetKind,
    TargetConversationConfig,
)
from tai42_kit.utils.data import get_compiled_jq

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX
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
from tai42_skeleton.operations.errors import (
    ConflictError,
    ForbiddenError,
    NotSupportedError,
    OperationFailed,
    UnavailableError,
)

if TYPE_CHECKING:
    from tai42_contract.conversations import Person

    from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager as _Manager
    from tai42_skeleton.conversations.mode import ConversationModeStore
    from tai42_skeleton.conversations.persons import ConversationPersonStore
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

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


async def _assert_target_exists(target_kind: str, target_name: str) -> None:
    """Existence only: the turn runs as the key, so the key's authority over the target is
    deliberately not checked here. An ``agent`` target must be registered; a ``tool`` target
    must resolve on the live tool registry. A miss is a loud 404, mirroring either side."""
    from tai42_skeleton.app import instance
    from tai42_skeleton.tools.binding import UnknownToolError

    if target_kind == "agent":
        if target_name not in instance.app.agents.all_agents():
            raise NotFoundError(f"agent not found: {target_name!r}")
        return
    try:
        await instance.app.tools.get_tool(target_name)
    except UnknownToolError as exc:
        raise NotFoundError(f"tool not found: {target_name!r}") from exc


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
    principal in its own id, a channel thread names the medium's address — so the held
    threads cannot be re-keyed under the new door. The flip is refused rather than
    half-applied, and refused by the write itself so a first message opening a thread cannot
    slip in behind a separate check."""
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
    initial_mode: str = "agent",
    channel: str | None = None,
    our_identity: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """Create a conversation route from its flat parameters — an UPSERT, so this is the
    create path AND the edit path for a route of that name.

    ``initial_mode`` is the route's default control mode (``agent`` runs the turn, ``manual``
    suppresses it for an operator to answer) when a thread carries no per-thread override.

    ``execution_key`` is the api-key identity the turn runs AS; the caller must be allowed
    to delegate it and it must be usable by a tokenless fire, both decided BEFORE the write
    so a refusal leaves any existing row untouched. ``target_name`` must merely EXIST — the
    agent (``target_kind=agent``) or tool (``target_kind=tool``) — the key's live grants
    bound the turn at fire. A tool target's ``payload_expr``/``reply_expr`` jq programs, when
    given, are compiled here so an invalid one is refused at create, not at first message. A
    ``channel`` row's ``our_identity`` is stored canonicalized and must not already be routed
    on that channel. An edit that would change the ``door`` of a route already HOLDING
    threads is refused: the two doors key their threads differently, so the held threads
    cannot be re-keyed under the new door.
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
            initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
            execution_key=execution_key,
            channel=channel,
            our_identity=our_identity,
            callback_url=callback_url,
        )
    except ValueError as exc:
        raise BadRequestError(f"invalid conversation route: {exc}") from exc

    manager = _require_backend()

    await _assert_target_exists(create.target_kind, create.target_name)
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
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def get_conversation_message(route_name: str, message_id: str) -> dict[str, Any]:
    """One conversation answer record by ``message_id`` under ``route_name``.

    A missing record or one on another route is a 404. An admin reads the whole record; a
    non-admin reads the caller-safe projection, which withholds the internal detail of the
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

    Spec metadata only — the door parses its query at the HTTP edge."""

    page: int = Field(default=1, ge=1, le=MAX_THREAD_PAGE, description="1-based page number, newest activity first.")
    page_size: int = Field(
        default=50,
        ge=1,
        alias="pageSize",
        description=f"Items per page. A larger value is capped to {MAX_THREAD_PAGE_SIZE}, never refused.",
    )


class ThreadListQuery(ThreadWindowQuery):
    """The thread listing's optional filters on top of the shared window. Both are a BOUNDED
    app-side post-scan (no per-route+status index), so a filtered page may report ``truncated``.

    Spec metadata only — the door parses its query at the HTTP edge."""

    status: str | None = Field(
        default=None,
        description="Keep only threads whose newest record's delivery_status is this one "
        "(validated against the delivery-status vocabulary; an unknown value is a 400).",
    )
    address: str | None = Field(
        default=None,
        description="Keep only threads whose id client-address suffix contains this substring.",
    )


class TranscriptQuery(ThreadWindowQuery):
    """The transcript door's query on top of the shared window. ``thread_id`` is REQUIRED and
    holds at least one non-whitespace character — a client generated without it calls the door
    with no thread to read, and a blank one names no thread; both are answered 400."""

    page: int = Field(
        default=1, ge=1, le=MAX_THREAD_PAGE, description="1-based page number, in the requested ``order``."
    )
    thread_id: str = Field(min_length=1, pattern=r"\S", description="The thread to read, as the send door returned it.")
    order: TranscriptOrder = Field(
        default="asc",
        description="``asc`` reads the transcript oldest first; ``desc`` is the live-tail order.",
    )
    q: str | None = Field(
        default=None,
        description="Optional text filter — keep only records whose inbound text or answer "
        "contains this substring (a BOUNDED scan, so a filtered page may report ``truncated``).",
    )


class MessageSearchQuery(ThreadWindowQuery):
    """The route-scoped message-search door's query. ``q`` is REQUIRED and holds at least one
    non-whitespace character; the search is a BOUNDED scan across the route's threads, so a page
    may report ``truncated``.

    Spec metadata only — the door parses its query at the HTTP edge."""

    q: str = Field(min_length=1, pattern=r"\S", description="The text to search the route's records for.")


class ThreadDeleteQuery(BaseModel):
    """The thread-delete door's ``?thread_id=`` query. ``thread_id`` is REQUIRED and holds at
    least one non-whitespace character — a client generated without it calls the door with no
    thread to forget, and a blank one names no thread; both are answered 400.

    Spec metadata only — the door parses its query at the HTTP edge."""

    thread_id: str = Field(
        min_length=1, pattern=r"\S", description="The thread to forget, as the send door returned it."
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
    """The uniform not-found the thread reads give for a thread that is absent, expired under
    the retention TTL, or keyed to another route."""
    return NotFoundError(f"conversation thread not found: {thread_id!r}")


def _person_store() -> ConversationPersonStore:
    """The person store over the live conversations settings. Called only after
    :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.persons import ConversationPersonStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    return ConversationPersonStore(ConversationsSettings())


def _person_routes(person: Person) -> set[str]:
    """Every route name the person has written under, straight off its address rows — the
    routes whose indexes the aggregated transcript spans and the authz check reads."""
    return {route for address in person.addresses for route in address.routes}


def _parse_status_filter(status: str | None):
    """The delivery-status set a thread listing filters on, validated against the enum per the
    observability ``parse_run_filter`` precedent — an unknown value is a loud 400, never a
    silent-ignore. A blank/absent value is no filter."""
    if status is None or not status.strip():
        return None
    from tai42_skeleton.conversations.models import DeliveryStatus

    valid = {member.value for member in DeliveryStatus}
    if status not in valid:
        raise BadRequestError(f"status must be one of {sorted(valid)}: {status!r}")
    return frozenset({DeliveryStatus(status)})


def _parse_address_filter(address: str | None) -> str | None:
    """The client-address substring a thread listing filters on. A blank/absent value is no
    filter; otherwise the trimmed substring, matched literally against the thread-id suffix."""
    if address is None:
        return None
    trimmed = address.strip()
    return trimmed or None


@operation(
    summary="List a conversation route's threads",
    tags=["conversations"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=ThreadListQuery,
)
async def list_conversation_threads(
    route_name: str,
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """The threads of ``route_name``, newest activity first, one page at a time.

    A thread listing spans every caller and address on the route, so it is admin-only. Each
    item carries ``thread_id``, ``client_address``, ``message_count``
    and ``last_delivery_status`` summarized from the thread's newest readable record, plus
    ``last_activity_at`` — the route index's own score, which is what this listing SORTS by,
    so the moment shown and the position it is shown in always agree. That score is stamped
    when a record is created and again when its turn completes; a later delivery transition
    on the same record moves the record's ``updated_at`` but not the thread's activity.

    ``status`` (one of the delivery-status vocabulary) keeps only threads whose summary status
    matches; ``address`` keeps only threads whose id client-address suffix contains that
    substring. Neither has a direct index — the per-status indexes are GLOBAL over
    ``message_id``s, not per-route thread ids — so a filter is a BOUNDED app-side post-scan
    and a page that spent its scan budget answers ``truncated: true`` LOUDLY, never a silent
    cut. An unknown ``status`` is a loud 400.

    Authorization is decided BEFORE the route is looked up, so a non-admin is refused the
    same way whether the name routes or not — a 404-here/403-there pair would answer which
    route names exist to a caller with no business knowing. An unknown route is a loud 404
    to an admin; a ``page`` or ``page_size`` below 1, or a ``page`` above the served
    maximum, is a 400. Returns
    ``{"items", "total", "page", "page_size", "next_page", "truncated"}``, where ``total``
    counts the route's indexed threads for an unfiltered listing, or the matches the bounded
    scan found for a filtered one.
    """
    _validate_route_name(route_name)
    manager = _require_backend()
    require_admin(await resolve_caller())
    await _require_route(manager, route_name)
    offset, limit = _page_bounds(page, page_size)
    status_filter = _parse_status_filter(status)
    address_filter = _parse_address_filter(address)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    listed = await ConversationRecordStore(ConversationsSettings()).list_route_threads(
        route_name, offset=offset, limit=limit, status=status_filter, address=address_filter
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
        "truncated": listed.truncated,
    }


@operation(
    summary="Read a conversation thread's transcript",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=TranscriptQuery,
)
async def get_conversation_thread(
    route_name: str, thread_id: str, page: int = 1, page_size: int = 50, order: str = "asc", q: str | None = None
) -> dict[str, Any]:
    """One thread's records under ``route_name``, one page at a time.

    ``order`` picks the direction: ``asc`` (the default) reads the transcript oldest first,
    ``desc`` reads it newest first, which is the order a live tail wants because page 1 then
    always holds the latest messages. ``page``/``page_size`` window that order from its own
    end, so page 1 of ``desc`` is the newest page and never the oldest.

    ``q`` filters to records whose inbound text or answer contains that substring — a BOUNDED
    scan (the searched text lives inside the record content blob), so a page that spent its
    scan budget answers ``truncated: true`` LOUDLY. A ``q`` read never 404s: the unknown-thread
    404 below is gated on an UNFILTERED read, so under ``q`` an unknown thread and one that
    matched nothing alike read as an EMPTY page.

    An admin reads whole records; a non-admin reads the caller-safe projection, which
    withholds the internal detail of the route key's run. An unknown ``route_name`` is a loud
    404. A thread that is absent or keyed to another route answers the uniform thread
    not-found. A ``page`` or ``page_size`` below 1, a ``page`` above the served maximum, a
    blank ``thread_id`` or an unknown ``order`` is a 400.

    A thread the index still holds but whose rows have expired under the retention TTL is
    NOT that 404: it reads as an empty page carrying the indexed ``total``, until the prune
    pass reclaims the members and the thread becomes unknown.

    Returns ``{"items", "total", "page", "page_size", "next_page", "order", "truncated"}``,
    where ``total`` counts the thread's indexed records for an unfiltered read, or the matches
    a bounded ``q`` scan found.
    """
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    if order not in TRANSCRIPT_ORDERS:
        raise BadRequestError(f"order must be one of {list(TRANSCRIPT_ORDERS)}, got {order!r}")
    offset, limit = _page_bounds(page, page_size)
    needle = q if (q is not None and q.strip()) else None
    manager = _require_backend()
    caller = await resolve_caller()
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    if thread_id.startswith(PERSON_THREAD_PREFIX):
        return await _read_person_thread(
            manager, route_name, thread_id, caller, page=page, offset=offset, limit=limit, order=order, q=needle
        )
    await _require_route(manager, route_name)
    transcript = await ConversationRecordStore(ConversationsSettings()).list_thread_records(
        route_name, thread_id, offset=offset, limit=limit, newest_first=order == "desc", q=needle
    )
    # An unknown/expired thread reads as total==0. Under a ``q`` search total is the MATCH
    # count, so an unfiltered read alone turns an empty index into the uniform 404; a search
    # that found nothing in a thread that exists returns an empty page.
    if needle is None and transcript.total == 0:
        raise _thread_not_found(thread_id)
    return _transcript_response(transcript, caller, page=page, limit=limit, order=order)


def _transcript_response(transcript, caller: Caller, *, page: int, limit: int, order: str) -> dict[str, Any]:
    """The shared transcript page shape both the route-keyed and the aggregated person read
    return: an admin reads whole records, a non-admin the caller-safe projection."""
    view = (lambda record: record.view()) if caller.is_admin else (lambda record: record.caller_view())
    return {
        "items": [view(record) for record in transcript.records],
        "total": transcript.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, transcript.total),
        "order": order,
        "truncated": transcript.truncated,
    }


async def _read_person_thread(
    manager: BaseConversationsManager,
    route_name: str,
    thread_id: str,
    caller: Caller,
    *,
    page: int,
    offset: int,
    limit: int,
    order: str,
    q: str | None = None,
) -> dict[str, Any]:
    """One page of a LINKED person's AGGREGATED transcript: the merged history across
    every route the person has written under, keyed by ``bridge:@person:{person_id}``.

    The supplied ``route_name`` must be one of the person's routes, while the fetch spans the
    indexes of ALL of them. An unknown ``route_name`` is a loud 404; a target mismatch, an
    unknown person, or a route the person never wrote under answers the uniform thread
    not-found. An empty aggregate is that 404 for an unfiltered read; under a ``q`` search it
    is an empty page (the person is real, the search simply matched nothing). An admin reads
    whole records; a non-admin the caller-safe projection."""
    person_id = thread_id[len(PERSON_THREAD_PREFIX) :]
    route = await _require_route(manager, route_name)
    person = await _person_store().get_by_id(person_id)
    if (
        person is None
        or (route.target_kind, route.target_name) != (person.target_kind, person.target_name)
        or route_name not in _person_routes(person)
    ):
        raise _thread_not_found(thread_id)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    transcript = await ConversationRecordStore(ConversationsSettings()).list_person_thread_records(
        sorted(_person_routes(person)), thread_id, offset=offset, limit=limit, newest_first=order == "desc", q=q
    )
    if q is None and transcript.total == 0:
        raise _thread_not_found(thread_id)
    return _transcript_response(transcript, caller, page=page, limit=limit, order=order)


@operation(
    summary="Search a conversation route's messages",
    tags=["conversations"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=MessageSearchQuery,
)
async def search_conversation_messages(route_name: str, q: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Every record on ``route_name`` whose inbound text or answer contains ``q``, across ALL
    the route's threads, one page at a time.

    The search spans every caller and address on the route, so it is admin-only, and each item
    is the WHOLE record (the same shape the transcript serves an admin). ``q`` is REQUIRED and
    non-blank. There is no per-route record index, so the search is a BOUNDED nested scan of
    the route's threads and their records; a page that spent its scan budget answers
    ``truncated: true`` LOUDLY, never a silent cut.

    Authorization is decided BEFORE the route is looked up, so a non-admin is refused the same
    way whether the name routes or not. An unknown route is a loud 404 to an admin; a blank
    ``q``, a ``page``/``page_size`` below 1, or a ``page`` above the served maximum, is a 400.
    Returns ``{"items", "total", "page", "page_size", "next_page", "truncated"}``, where
    ``total`` is the matches the bounded scan found."""
    _validate_route_name(route_name)
    if not q.strip():
        raise BadRequestError("q must be a non-blank search string")
    manager = _require_backend()
    require_admin(await resolve_caller())
    await _require_route(manager, route_name)
    offset, limit = _page_bounds(page, page_size)
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    found = await ConversationRecordStore(ConversationsSettings()).search_route_messages(
        route_name, offset=offset, limit=limit, q=q
    )
    return {
        "items": [record.view() for record in found.records],
        "total": found.total,
        "page": page,
        "page_size": limit,
        "next_page": _next_page(page, limit, found.total),
        "truncated": found.truncated,
    }


@operation(
    summary="List failed conversation deliveries",
    tags=["conversations"],
    errors=[ForbiddenError, NotSupportedError],
)
async def list_failed_conversations() -> dict[str, Any]:
    """Every answer record whose delivery ended ``failed``. The listing spans every route and
    caller, so it is admin-only. Returns ``{"items", "total"}``."""
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


async def _thread_delete_routes(thread_id: str, route_name: str) -> list[str]:
    """The route indexes ``thread_id`` is forgotten across. A route-keyed thread lives under
    its one ``route_name`` and its id MUST carry that route's ``bridge:{route_name}:`` prefix
    — a route slug carries no ``:``, so the prefix is unambiguous; the check is the sole guard
    stopping a delete on one route from reaching another route's thread memory by id, and a
    mismatch is a loud 400. A LINKED person's aggregated thread (``bridge:@person:{id}``) lives
    across EVERY route the person wrote under, and the supplied ``route_name`` must be one of
    them (else a loud 404), so the delete reaches only a thread the person actually holds and
    every route index carrying it is reclaimed (else a member strands under one). Caller
    authority is the door's grantable write action — the same grant that creates a route —
    never derived here."""
    if not thread_id.startswith(PERSON_THREAD_PREFIX):
        prefix = f"{BRIDGE_THREAD_PREFIX}{route_name}:"
        if not thread_id.startswith(prefix):
            raise BadRequestError(
                f"thread_id {thread_id!r} is not a thread of route {route_name!r}: it must start with {prefix!r}"
            )
        return [route_name]
    person = await _person_store().get_by_id(thread_id[len(PERSON_THREAD_PREFIX) :])
    if person is None or route_name not in _person_routes(person):
        raise _thread_not_found(thread_id)
    return sorted(_person_routes(person))


async def _delete_thread_checkpoint(thread_id: str) -> None:
    """Delete ``thread_id``'s agent checkpoint on the configured provider — the run's actual
    memory, reached the way the retention sweep reaches it. Every provider's saver must expose
    ``adelete_thread``; one that does not is a loud 501, never a silent skip that would leave
    the forgotten thread's memory behind."""
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
    from tai42_kit.llm.settings import llm_provider_settings

    settings = llm_provider_settings()
    saver = await checkpoint_registry().get_checkpointer(
        provider=settings.checkpoint, conn_string=settings.checkpoint_conn_string
    )
    adelete_thread = getattr(saver, "adelete_thread", None)
    if adelete_thread is None:
        raise NotSupportedError(
            f"checkpoint provider {settings.checkpoint!r} exposes no adelete_thread, so a conversation "
            "thread's memory cannot be forgotten on it"
        )
    await adelete_thread(thread_id)


@operation(
    summary="Delete a conversation thread",
    tags=["conversations"],
    errors=[BadRequestError, ConflictError, NotFoundError, NotSupportedError, UnavailableError],
    query_model=ThreadDeleteQuery,
)
async def delete_conversation_thread(route_name: str, thread_id: str) -> dict[str, Any]:
    """Forget ONE conversation thread: its agent checkpoint, its answer records and its thread
    indexes, so a later message on the same address starts a memory the deleted turns never
    touched. A LINKED person's aggregated ``bridge:@person:{id}`` thread is forgotten across
    every route index it spans.

    Forgetting is ABSOLUTE: a valid id on its own route always succeeds, even when nothing is
    stored. An aged-out thread whose answer records already expired under the retention TTL,
    or one never seen, answers ``removed=0`` and never a 404 — and its checkpoint is deleted
    regardless, because that memory defaults to keep-forever and is otherwise left behind once
    the records lapse. A route-keyed id MUST carry the route's ``bridge:{route_name}:`` prefix;
    an id belonging to another route is a loud 400, the sole guard stopping a delete on one
    route from wiping another route's memory. A person ``bridge:@person:{id}`` thread whose
    person is unknown, or whose ``route_name`` is not one of the person's routes, is a loud
    404. A turn IN FLIGHT on the thread (an ``accepted`` intake still holding a live lease) is
    a 409: its completion re-writes checkpoint state and re-stamps the indexes behind the
    delete, half-forgetting the memory — retry once it drains. The guard is re-checked under
    the FIFO, so a turn admitted while the lock is being taken is refused before any teardown,
    never left to re-create memory behind the delete. A ``route_name`` that is not a valid
    slug, or a blank ``thread_id``, is a 400.

    Caller authority is the door's grantable write action — the SAME write grant that creates
    a route deletes routes and forgets threads — never a per-thread owner check.

    The teardown runs in an order an interruption cannot strand: the checkpoint FIRST — its
    memory is reachable only through the indexes once this call returns, so it must go while
    the indexes still name the thread to bring a re-run back — then the records and the indexes
    together, the index being the durable marker of an owed reclamation and torn down LAST.
    Idempotent under re-run, so a partial completion is FINISHED by a retry.

    The whole teardown runs under the thread's per-thread FIFO (the lock an operator send and
    an in-flight turn take), so an operator send already in flight on the thread within this
    worker drains BEFORE the delete rather than half-behind it; a full queue is the loud,
    retriable 503 that FIFO raises before anything is torn down. As a live-caller sync door the
    acquisition is bounded by ``sync_door_wait_seconds``: a wait past it — behind a turn possibly
    HITL-paused on another worker — is the loud, retriable 503 ``ThreadBusyError`` rather than a
    block past the proxy timeout.

    Returns ``{"removed", "route_name", "thread_id"}``, where ``removed`` counts the answer
    records this call deleted (0 when a prior run already cleared them, when their rows had
    expired under the retention TTL, or when the id was never stored)."""
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    _require_backend()
    from tai42_skeleton.conversations.caps import get_turn_caps
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    store = ConversationRecordStore(ConversationsSettings())
    route_names = await _thread_delete_routes(thread_id, route_name)
    in_flight_409 = f"conversation thread {thread_id!r} has a turn in flight; retry once it drains"
    if await store.thread_has_live_intake(thread_id):
        raise ConflictError(in_flight_409)
    caps = get_turn_caps()
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        # Re-check UNDER the FIFO: an intake admitted between the outer check and taking the
        # lock is durably visible here, so its turn — queued behind this lock — is refused
        # before any teardown, never left to run after the delete and re-create the checkpoint.
        if await store.thread_has_live_intake(thread_id):
            raise ConflictError(in_flight_409)
        await _delete_thread_checkpoint(thread_id)
        removed = 0
        for name in route_names:
            removed += await store.drop_thread(name, thread_id)
    return {"removed": removed, "route_name": route_name, "thread_id": thread_id}


@operation(
    summary="Delete a conversation person",
    tags=["conversations"],
    errors=[BadRequestError, ConflictError, NotSupportedError, UnavailableError],
)
async def delete_conversation_person(person_id: str) -> dict[str, Any]:
    """Erase a LINKED person ENTIRELY, forgetting every store that names it:

    - the person's aggregated ``bridge:@person:{person_id}`` thread — its agent checkpoint,
      and across EVERY route the person wrote under its answer records, per-thread transcript
      indexes, route-thread index memberships and per-thread mode override (the thread-delete
      machinery, reused);
    - the person row ``conversations:person:{person_id}``;
    - every ``person_index`` ``door_address_key → person_id`` mapping of its addresses.

    Caller authority is the door's grantable ``write`` action — the same grant that forgets a
    thread. IDEMPOTENT and retryable: the person row is the durable marker of an owed erase and
    is deleted LAST (atomically with its index mappings), so an interruption re-reads it and a
    retry FINISHES. A person that is already gone (a retry, or one that never existed) is not a
    404 — its aggregated checkpoint is forgotten regardless (it defaults to keep-forever) and
    the call answers ``erased=false``. A turn IN FLIGHT on the aggregated thread is a 409
    (retry once it drains), re-checked under the per-thread FIFO before any teardown so an
    admitted turn cannot re-create memory behind the erase; a full queue is the retriable 503.
    As a live-caller sync door the acquisition is bounded by ``sync_door_wait_seconds`` (both the
    linked and the already-gone branch): a wait past it — behind a turn possibly HITL-paused on
    another worker — is the loud, retriable 503 ``ThreadBusyError`` rather than a block past the
    proxy timeout. A blank ``person_id`` is a 400; no backend is a loud 501.

    Returns ``{"person_id", "removed", "erased"}``, where ``removed`` counts the answer records
    this call deleted across the person's routes and ``erased`` says whether THIS call removed
    the person row."""
    if not person_id.strip():
        raise BadRequestError("person_id must be a non-blank person identifier")
    _require_backend()
    from tai42_skeleton.conversations.caps import get_turn_caps
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    store = ConversationRecordStore(ConversationsSettings())
    person_store = _person_store()
    thread_id = f"{PERSON_THREAD_PREFIX}{person_id}"
    person = await person_store.get_by_id(person_id)
    caps = get_turn_caps()
    if person is None:
        # Already erased (a retry) or never a person: reachable from the id alone, its
        # aggregated checkpoint is forgotten regardless — keep-forever memory left behind
        # otherwise — and nothing route-scoped remains to reclaim. The checkpoint delete runs
        # under the per-thread FIFO and cross-worker lease, exactly as the linked branch does,
        # so an in-flight turn on the aggregated thread cannot re-fork the memory behind it.
        caps.reserve_thread_slot(thread_id)
        async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
            await _delete_thread_checkpoint(thread_id)
        return {"person_id": person_id, "removed": 0, "erased": False}
    route_names = sorted(_person_routes(person))
    in_flight_409 = f"conversation person {person_id!r} has a turn in flight; retry once it drains"
    if await store.thread_has_live_intake(thread_id):
        raise ConflictError(in_flight_409)
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        # Re-check UNDER the FIFO, exactly as the thread delete does: an intake admitted while
        # the lock was being taken is refused before any teardown, never left to re-stamp a
        # route index behind the erase.
        if await store.thread_has_live_intake(thread_id):
            raise ConflictError(in_flight_409)
        await _delete_thread_checkpoint(thread_id)
        removed = 0
        for name in route_names:
            removed += await store.drop_thread(name, thread_id)
        # The row (with its index mappings) goes LAST, atomically: it is the durable marker a
        # retry finds the owed work by, so an interrupted run re-reads it and finishes.
        erased, _fields = await person_store.erase(person)
    return {"person_id": person_id, "removed": removed, "erased": erased}


# -- operator send + per-thread mode ------------------------------------------------------
#
# The operator surface over a live thread: send a message BY HAND into it (no turn runs), and
# read or override the thread's control mode. All three carry the thread-belongs-to-route
# guard the thread delete uses, so one route can never reach another's thread by id.


def _record_store() -> ConversationRecordStore:
    """The answer/record store over the live conversations settings. Called only after
    :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    return ConversationRecordStore(ConversationsSettings())


def _mode_store() -> ConversationModeStore:
    """The per-thread mode-override store over the live conversations settings. Called only
    after :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.mode import ConversationModeStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    return ConversationModeStore(ConversationsSettings())


async def _route_keyed_target(named_route: ConversationRoute, thread_id: str, address: str | None) -> tuple[str, str]:
    """The ``(route_name, client_address)`` a route-keyed thread's operator send targets: the
    one route it lives on and the address embedded in its id. The id MUST carry the route's
    ``bridge:{route_name}:`` prefix (the sole guard against reaching another route's thread) —
    a mismatch is a loud 400. An explicit ``address`` must equal that embedded address."""
    prefix = f"{BRIDGE_THREAD_PREFIX}{named_route.route_name}:"
    if not thread_id.startswith(prefix):
        raise BadRequestError(
            f"thread_id {thread_id!r} is not a thread of route {named_route.route_name!r}: "
            f"it must start with {prefix!r}"
        )
    embedded = thread_id[len(prefix) :]
    if not embedded.strip():
        raise BadRequestError(f"thread_id {thread_id!r} carries no address after its route prefix")
    if address is not None and address != canonical_address(embedded):
        raise BadRequestError(f"address {address!r} is not the address of thread {thread_id!r}")
    return named_route.route_name, embedded


async def _person_target(
    store: ConversationRecordStore,
    named_route: ConversationRoute,
    thread_id: str,
    address: str | None,
) -> tuple[str, str]:
    """The ``(route_name, client_address)`` a LINKED person's aggregated-thread operator send
    targets. The named route must be one of the person's routes (else a uniform thread
    not-found). An explicit ``address`` must be one of the person's addresses (else a loud
    400), and its send route is the named route when the address wrote under it, else the
    address's own first route. With no explicit address the target is the person's NEWEST
    record — its route and client_address — so the reply returns where they last wrote from;
    an empty thread offers none, which is a loud 400 asking for an explicit address."""
    person_id = thread_id[len(PERSON_THREAD_PREFIX) :]
    person = await _person_store().get_by_id(person_id)
    if (
        person is None
        or (named_route.target_kind, named_route.target_name) != (person.target_kind, person.target_name)
        or named_route.route_name not in _person_routes(person)
    ):
        raise _thread_not_found(thread_id)
    if address is not None:
        match = next((row for row in person.addresses if row.address == address), None)
        if match is None:
            raise BadRequestError(f"address {address!r} is not one of the thread's person addresses")
        route_name = named_route.route_name if named_route.route_name in match.routes else sorted(match.routes)[0]
        return route_name, match.address
    newest = await store.list_person_thread_records(
        sorted(_person_routes(person)), thread_id, offset=0, limit=1, newest_first=True
    )
    if not newest.records:
        raise BadRequestError(
            f"thread {thread_id!r} holds no record to infer a send address from; pass an explicit address"
        )
    record = newest.records[0]
    return record.route_name, record.client_address


async def _resolve_operator_target(
    manager: _Manager,
    store: ConversationRecordStore,
    named_route: ConversationRoute,
    thread_id: str,
    address: str | None,
) -> tuple[ConversationRoute, str]:
    """The ``(target route, client_address)`` an operator message is built and delivered
    against. A route-keyed thread targets its own route and embedded address; a person thread
    targets the address's (or newest record's) route — looked up live, so a deleted route is a
    loud 404 rather than a send with a stale identity. ``address``, when given, is already
    canonicalized by the caller — the only place a MALFORMED address maps to a 400 —
    so a ``ValueError`` from a store read here is server-side corruption, not a client error."""
    if thread_id.startswith(PERSON_THREAD_PREFIX):
        route_name, client_address = await _person_target(store, named_route, thread_id, address)
    else:
        route_name, client_address = await _route_keyed_target(named_route, thread_id, address)
    target_route = named_route if route_name == named_route.route_name else await manager.get_route(route_name)
    if target_route is None:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    return target_route, client_address


@operation(
    summary="Send an operator message into a conversation thread",
    tags=["conversations"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError, OperationFailed, UnavailableError],
)
async def send_conversation_thread_message(
    route_name: str, thread_id: str, text: str, address: str | None = None
) -> dict[str, Any]:
    """Send a message BY HAND into ``thread_id`` on ``route_name`` as the route identity, and
    return ``{"message_id", "thread_id"}``. No turn runs: the message is stored already
    ``answered`` and delivered through the same machine a produced answer takes.

    Allowed in either mode and it never flips the mode. Blank ``text`` is a loud 400, and a
    present-but-blank ``address`` is a 400. The thread-belongs-to-route guard is the thread
    delete's: a route-keyed id must carry the route's ``bridge:{route_name}:`` prefix (400
    otherwise), a person thread must be on the named route (404 otherwise). ``address`` picks
    the send target on a LINKED person's aggregated thread — it must be one of the person's
    addresses (400 otherwise); with no ``address`` the target is the thread's newest record,
    and an empty person thread with no ``address`` is a 400. For an agent target that holds
    thread memory the message is appended to the thread's checkpoint as an ``assistant`` reply
    BEFORE the record is created; an append that fails is a loud 500 and no record is created.
    The send takes the thread's per-thread FIFO, so it waits behind an in-flight turn and
    never interleaves it; a full queue is a loud, retriable 503. As a live-caller sync door the
    wait to acquire the slot is bounded by ``sync_door_wait_seconds``: a wait past it — behind a
    turn possibly HITL-paused on another worker — is the loud, retriable 503 ``ThreadBusyError``
    rather than a block past the proxy timeout.

    Caller authority is the door's grantable ``write`` action — the same write grant that
    forgets threads — and the record names the calling operator. An unauthenticated caller
    (access control disabled or unbound) is a loud 501, since an operator action must be
    attributable."""
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    if not text.strip():
        raise BadRequestError("text must be a non-blank message to send")
    manager = _require_backend()
    named_route = await _require_route(manager, route_name)
    caller = await resolve_caller()
    operator_principal = caller.caller_id
    if not (operator_principal and operator_principal.strip()):
        raise NotSupportedError(
            "an operator send needs an authenticated caller principal to attribute the message to, and this "
            "deployment resolved none; enable access control"
        )
    store = _record_store()
    if address is not None:
        try:
            address = canonical_address(address)
        except ValueError as exc:
            raise BadRequestError(f"invalid address: {exc}") from exc
    target_route, client_address = await _resolve_operator_target(manager, store, named_route, thread_id, address)
    from tai42_skeleton.conversations.turn import OperatorAppendError, operator_send

    try:
        message_id = await operator_send(
            route=target_route,
            thread_id=thread_id,
            client_address=client_address,
            text=text,
            operator_principal=operator_principal,
        )
    except OperatorAppendError as exc:
        raise OperationFailed(str(exc)) from exc
    return {"message_id": message_id, "thread_id": thread_id}


@operation(
    summary="Read a conversation thread's control mode",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def get_conversation_thread_mode(route_name: str, thread_id: str) -> dict[str, Any]:
    """The mode in force for ``thread_id`` on ``route_name`` and where it comes from:
    ``{"mode", "source"}``, ``source`` being ``thread`` for a per-thread override and
    ``route`` for the no-override default. A route-keyed thread's default is the route's
    ``initial_mode``; a LINKED person's aggregated thread defaults to ``manual`` when ANY
    route the person spans defaults to ``manual``, else ``agent``.

    The thread-belongs-to-route guard is the thread delete's: a route-keyed id off the route
    is a 400, a person thread off the named route a 404. An unknown route is a loud 404; a
    blank ``thread_id`` a 400."""
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    manager = _require_backend()
    await _require_route(manager, route_name)
    route_names = await _thread_delete_routes(thread_id, route_name)
    override = await _mode_store().get_mode(thread_id)
    if override is not None:
        return {"mode": override, "source": "thread"}
    from tai42_skeleton.conversations.mode import default_mode_for_routes

    return {"mode": await default_mode_for_routes(manager, route_names), "source": "route"}


@operation(
    summary="Set a conversation thread's control mode",
    tags=["conversations"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def set_conversation_thread_mode(route_name: str, thread_id: str, mode: str) -> dict[str, Any]:
    """Set the per-thread mode override for ``thread_id`` on ``route_name`` to ``mode`` (one
    of ``agent``/``manual``), returning ``{"route_name", "thread_id", "mode", "source"}`` with
    ``source`` always ``thread`` — a set writes an override.

    This is the door an EXTERNAL/programmatic caller names a thread through; an agent flipping
    its OWN live conversation uses the ``set_conversation_mode`` builtin instead, which reads
    the current thread from the turn context rather than naming it.

    Caller authority is the door's grantable ``write`` action. The thread-belongs-to-route
    guard is the thread delete's: a route-keyed id off the route is a 400, a person thread off
    the named route a 404. An unknown route is a loud 404; a blank ``thread_id`` or a ``mode``
    outside the vocabulary is a 400."""
    _validate_route_name(route_name)
    if not thread_id.strip():
        raise BadRequestError("thread_id must be a non-blank thread identifier")
    if mode not in CONVERSATION_MODES:
        raise BadRequestError(f"mode must be one of {list(CONVERSATION_MODES)}: {mode!r}")
    manager = _require_backend()
    await _require_route(manager, route_name)
    await _thread_delete_routes(thread_id, route_name)
    stored = await _mode_store().set_mode(thread_id, mode)
    return {"route_name": route_name, "thread_id": thread_id, "mode": stored, "source": "thread"}


# -- per-target conversation config --------------------------------------------------------
#
# The per-target multichannel opt-in + first-contact greeting, keyed ``(target_kind,
# target_name)``. Inert operator config: nothing here reads the stored value — the accept
# path and the pairing tool consume it in a later step.

_TARGET_KINDS = get_args(ConversationTargetKind)


def _validate_target_key(target_kind: str, target_name: str) -> None:
    """A well-formed config key: a known ``target_kind`` and a non-blank ``target_name``. A
    malformed key is the caller's own 400, told apart from a well-formed key that names no
    stored config (a 404)."""
    if target_kind not in _TARGET_KINDS:
        raise BadRequestError(f"target_kind must be one of {list(_TARGET_KINDS)}: {target_kind!r}")
    if not target_name.strip():
        raise BadRequestError("target_name must be a non-blank target identifier")


def _config_store() -> ConversationTargetConfigStore:
    """The config store over the live conversations settings. Called only after
    :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.settings import ConversationsSettings
    from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

    return ConversationTargetConfigStore(ConversationsSettings())


@operation(summary="List conversation target configs", tags=["conversations"], errors=[NotSupportedError])
async def list_conversation_configs() -> dict[str, Any]:
    """Every stored per-target conversation config. Returns ``{"items", "total"}``."""
    _require_backend()
    configs = await _config_store().list()
    items = [config.model_dump(mode="json") for config in configs.values()]
    return {"items": items, "total": len(items)}


@operation(
    summary="Get a conversation target config",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def get_conversation_config(target_kind: str, target_name: str) -> dict[str, Any]:
    """One per-target config by ``(target_kind, target_name)``. An unknown key is a loud
    404; a key whose ``target_kind`` is not a known kind, or whose ``target_name`` is blank,
    is a 400."""
    _validate_target_key(target_kind, target_name)
    _require_backend()
    config = await _config_store().get(target_kind, target_name)
    if config is None:
        raise NotFoundError(f"conversation config not found: {target_kind}/{target_name}")
    return config.model_dump(mode="json")


@operation(
    summary="Create or replace a conversation target config",
    tags=["conversations"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=TargetConversationConfig,
)
async def set_conversation_config(
    target_kind: str,
    target_name: str,
    multichannel: bool = False,
    greeting_template: str | None = None,
) -> dict[str, Any]:
    """Create or replace the per-target config for ``(target_kind, target_name)`` — an
    UPSERT, so this is the create path AND the edit path for a config of that key.

    The target must merely EXIST — the agent (``target_kind=agent``) or tool
    (``target_kind=tool``) — exactly as the route create checks it. ``greeting_template``
    may reference at most the ``{pairing_code}`` placeholder and a blank template is refused
    (``null`` = no greeting), both enforced by the model. No key is bound and no authority
    delegated: this is inert operator config. Returns
    ``{"created", "target_kind", "target_name", "config"}``.
    """
    # Validate the whole body at the operation, not the edge: the MCP tool and a direct
    # call take these flat parameters and bypass the HTTP extractor.
    try:
        config = TargetConversationConfig(
            target_kind=target_kind,  # pyright: ignore[reportArgumentType]
            target_name=target_name,
            multichannel=multichannel,
            greeting_template=greeting_template,
        )
    except ValueError as exc:
        raise BadRequestError(f"invalid conversation config: {exc}") from exc
    _require_backend()
    await _assert_target_exists(config.target_kind, config.target_name)
    created = await _config_store().upsert(config)
    return {
        "created": created,
        "target_kind": config.target_kind,
        "target_name": config.target_name,
        "config": config.model_dump(mode="json"),
    }


@operation(
    summary="Delete a conversation target config",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def delete_conversation_config(target_kind: str, target_name: str) -> dict[str, Any]:
    """Delete the per-target config for ``(target_kind, target_name)``. An unknown key is a
    loud 404; a malformed key is a 400. Returns ``{"removed", "target_kind", "target_name"}``."""
    _validate_target_key(target_kind, target_name)
    _require_backend()
    removed = await _config_store().delete(target_kind, target_name)
    if not removed:
        raise NotFoundError(f"conversation config not found: {target_kind}/{target_name}")
    return {"removed": True, "target_kind": target_kind, "target_name": target_name}
