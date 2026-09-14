"""The operator send door: resolve the send target on a route-keyed or linked-person thread,
validate the optional rich-send parts, and deliver a by-hand message as the route identity."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.conversations import AnswerPart, ConversationRoute
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import NotSupportedError, OperationFailed, UnavailableError
from tai42_skeleton.operations.response_models_group_a import ThreadMessageAck

from .backend import _person_routes, _record_store, _require_backend, _require_route, _thread_not_found
from .models import _OPTIONS_ADAPTER, _SECTIONS_ADAPTER, ThreadMessageSend
from .routes import _validate_route_name

if TYPE_CHECKING:
    from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager as _Manager
    from tai42_skeleton.conversations.records import ConversationRecordStore


# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``/``_person_store``.
_pkg = sys.modules["tai42_skeleton.operations.conversations"]


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
    person = await _pkg._person_store().get_by_id(person_id)
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
    request_model=ThreadMessageSend,
    response_model=ThreadMessageAck,
)
async def send_conversation_thread_message(
    route_name: str,
    thread_id: str,
    text: str,
    address: str | None = None,
    media: list[dict[str, Any]] | None = None,
    options: list[dict[str, Any]] | None = None,
    # Appended after the earlier rich fields so their positional slots are unchanged —
    # the release API gate treats a moved positional parameter as breaking.
    template: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
    sections: list[dict[str, Any]] | None = None,
    header: dict[str, Any] | None = None,
    footer: str | None = None,
) -> dict[str, Any]:
    """Send a message BY HAND into ``thread_id`` on ``route_name`` as the route identity, and
    return ``{"message_id", "thread_id"}``. No turn runs: the message is stored already
    ``answered`` and delivered through the same machine a produced answer takes.

    ``media`` (a list of ``{"kind", "url", "caption"?, "filename"?}`` display items),
    ``location`` (a shared map pin ``{"latitude", "longitude", "name"?, "address"?}``),
    ``template`` (a pre-approved ``{"name", "language", "header_media"?, "body_parameters"?,
    "buttons"?}`` out-of-window template), ``options`` (a list of FLAT tappable option objects —
    each a ``{"kind": "reply", "text"}`` reply or a ``{"kind": "link", "label", "url"}`` link
    action), ``sections`` (the SECTIONED tappable-options alternative — titled groups of reply
    rows), ``header``/``footer`` (a media header / trailing line composing an interactive
    message) and ``schema`` (an ask-less form's
    answer schema — the channel renders ``text`` as the form's prompt, and the participant's
    submission enters the conversation as an ordinary inbound message) are OPTIONAL
    richer-send forms — FULL parity with the flow answer path's ``AnswerPart`` vocabulary —
    delivered ALONGSIDE ``text`` — the message is then stored and delivered as one rich
    part, exactly as a produced rich answer is, including the delivery machine's capability
    gate (a channel that does not advertise the matching ``supports_*_notifications`` flag
    never receives the part; the record fails loudly instead of the field dropping). A
    contract-invalid value (an empty list/dict, an over-cap value, or a combination the shared
    composition matrix refuses — ``options`` XOR ``sections``, ``schema`` excludes both,
    ``header``/``footer`` require a choice surface, ``template`` standalone) is a loud 400;
    omit them all for a plain text send.

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
    # Coerce and validate the rich fields up front, so a bad value is
    # a clean 400 here rather than a 500 from deep in the send. Constructing the AnswerPart
    # the send will build applies every contract check (empty list/dict, over-cap, and the
    # shared composition matrix — options XOR sections, schema excludes both, header/footer
    # require a choice surface, template standalone) — reused, never duplicated here.
    media_items: list[MediaItem] | None = None
    template_item: ChannelTemplate | None = None
    option_items: list[Option] | None = None
    location_item: LocationElement | None = None
    section_items: list[OptionSection] | None = None
    header_item: MediaItem | None = None
    if (
        media is not None
        or template is not None
        or options is not None
        or schema is not None
        or location is not None
        or sections is not None
        or header is not None
        or footer is not None
    ):
        try:
            media_items = (
                [item if isinstance(item, MediaItem) else MediaItem.model_validate(item) for item in media]
                if media is not None
                else None
            )
            template_item = ChannelTemplate.model_validate(template) if template is not None else None
            # Coerce the raw option dicts into the typed discriminated union, then let AnswerPart
            # apply the cross-field contract rules over the whole part.
            option_items = _OPTIONS_ADAPTER.validate_python(options) if options is not None else None
            location_item = LocationElement.model_validate(location) if location is not None else None
            section_items = _SECTIONS_ADAPTER.validate_python(sections) if sections is not None else None
            header_item = (
                (header if isinstance(header, MediaItem) else MediaItem.model_validate(header))
                if header is not None
                else None
            )
            AnswerPart(
                message=text,
                media=media_items,
                template=template_item,
                options=option_items,
                location=location_item,
                sections=section_items,
                header=header_item,
                footer=footer,
                schema=schema,
            )
        except (ValidationError, ValueError) as exc:
            raise BadRequestError(f"invalid rich-send fields: {exc}") from exc
    manager = _require_backend()
    named_route = await _require_route(manager, route_name)
    caller = await _pkg.resolve_caller()
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
            media=media_items,
            template=template_item,
            options=option_items,
            location=location_item,
            sections=section_items,
            header=header_item,
            footer=footer,
            schema=schema,
        )
    except OperatorAppendError as exc:
        raise OperationFailed(str(exc)) from exc
    return {"message_id": message_id, "thread_id": thread_id}
