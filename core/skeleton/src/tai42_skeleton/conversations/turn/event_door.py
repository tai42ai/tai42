"""The event door ``submit_event`` — a structured event as a turn on an existing thread."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from tai42_contract.conversations import ConversationEventSubmission, ConversationRoute

from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.caps import AddressAdmission, AddressRateLimitedError, get_turn_caps
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.api_wait import ApiSubmitResult, _api_wait_or_callback
from tai42_skeleton.conversations.turn.errors import (
    ConversationRouteResolutionError,
    EventTargetNotToolError,
    ThreadNotFoundError,
    UnauthenticatedApiCallerError,
)
from tai42_skeleton.conversations.turn.keys import _api_bucket_key, _api_client_address
from tai42_skeleton.conversations.turn.record import _new_record
from tai42_skeleton.conversations.turn.redrive import _resolve_stranded_intake
from tai42_skeleton.conversations.turn.routing import _Multichannel, _multichannel_context, _resolve_thread_id
from tai42_skeleton.conversations.turn.schedule import _schedule_turn, _spawn_intake_resolution


async def _get_event_route(route_name: str) -> ConversationRoute:
    """The route the event door runs on — door-agnostic (channel or api), tool-target only.
    A missing route is refused as unresolved (404); an AGENT target is refused with
    :class:`EventTargetNotToolError` (409) — an event has no rendered text to hand an agent."""
    route = await cache.get_conversations_manager().get_route(route_name)
    if route is None:
        raise ConversationRouteResolutionError(f"no conversation route named {route_name!r}")
    if route.target_kind != "tool":
        raise EventTargetNotToolError(
            f"conversation route {route_name!r} targets an agent; an event can only run a tool target"
        )
    return route


async def _resolve_event_thread(
    store: ConversationRecordStore,
    route: ConversationRoute,
    submission: ConversationEventSubmission,
    caller_principal: str,
) -> tuple[str, str, str | None, _Multichannel | None]:
    """Resolve the EXISTING thread an event enters, returning
    ``(thread_id, client_address, record_caller_principal, multichannel)``.

    Addressed by ``thread_id``: the id is verified live on the route's thread index and its
    latest record supplies the delivery ``client_address`` (a person-aggregated thread has
    no single address of its own). Addressed by ``address``: the address is composed exactly
    as the TARGET route's own door composes it — a channel route by
    :func:`canonical_address`, an api route by :func:`_api_client_address` (so by ``address``
    a caller reaches only its own api threads), then :func:`_resolve_thread_id` and the same
    existence check. A missing thread raises :class:`ThreadNotFoundError` (404) — an event
    never mints a thread. ``record_caller_principal`` honours ``_new_record``'s door
    invariant: ``None`` for a channel target, the qualifying principal for an api target.

    The multichannel context of the sending address is resolved and returned so the turn can
    read (never write) the linked person whose fields it carries into the tool payload."""
    if submission.thread_id is not None:
        thread_id = submission.thread_id.strip()
        latest = await store.latest_thread_record(route.route_name, thread_id)
        if latest is None:
            raise ThreadNotFoundError(f"no thread {thread_id!r} on conversation route {route.route_name!r}")
        record_caller_principal = latest.caller_principal if route.door == "api" else None
        multichannel = await _multichannel_context(
            route,
            door=route.door,
            channel=route.channel,
            our_identity=route.our_identity,
            address=latest.client_address,
            accountable=caller_principal,
        )
        return thread_id, latest.client_address, record_caller_principal, multichannel
    address = canonical_address(submission.address or "")
    if route.door == "channel":
        composed = address
        record_caller_principal = None
    else:
        composed = _api_client_address(caller_principal, address)
        record_caller_principal = caller_principal
    multichannel = await _multichannel_context(
        route,
        door=route.door,
        channel=route.channel,
        our_identity=route.our_identity,
        address=composed,
        accountable=caller_principal,
    )
    thread_id = await _resolve_thread_id(route, multichannel, composed)
    if not await store.thread_exists(route.route_name, thread_id):
        raise ThreadNotFoundError(f"no thread for address on conversation route {route.route_name!r}")
    return thread_id, composed, record_caller_principal, multichannel


async def submit_event(
    route_name: str,
    submission: ConversationEventSubmission,
    caller_principal: str | None,
) -> ApiSubmitResult:
    """Run a structured event as a turn ON an existing thread of ``route_name``.

    The event enters the thread's FIFO exactly like an inbound message — reserve the thread
    slot, persist a durable event record, run the tool target AS the route's execution key —
    and its answer is delivered by the TARGET route's door: a channel route texts the
    thread's address, an api route POSTs the route's signed callback (or returns the answer
    inline within ``wait_seconds``). It never mints a thread and never runs an agent target.

    ``caller_principal`` is MANDATORY: it is the accountable party the rate cap buckets on
    and the authorizing principal recorded on the event (``submitted_by``). Admission runs
    in the channel door's order — a cheap owner read-check, the rate cap, the thread slot,
    the durable record, and the idempotency claim LAST — so a refused admission (an unknown
    thread, a rate cap, a full queue) writes nothing and never burns the ``event_id`` key: a
    redelivery of a rate-capped event runs cleanly, and a redelivery of an ACCEPTED one
    returns the original turn's ``message_id`` (``202``) and starts no second turn."""
    if caller_principal is None or not caller_principal.strip():
        raise UnauthenticatedApiCallerError(
            f"event conversation route {route_name!r} needs an accountable caller principal and this "
            "deployment resolved none"
        )
    route = await _get_event_route(route_name)
    store = accessors._store()
    thread_id, client_address, record_caller_principal, multichannel = await _resolve_event_thread(
        store, route, submission, caller_principal
    )
    event = submission.event
    event_id = event.event_id

    # Cheap read-check before any admission write: an already-claimed event returns the
    # owning turn's identity and runs nothing (idempotency).
    owner = await store.get_event_owner(route_name, event_id)
    if owner is not None:
        return ApiSubmitResult(message_id=owner, thread_id=thread_id, answer=None)

    message_id = str(uuid4())
    caps = get_turn_caps()
    admission = caps.admit_address(_api_bucket_key(route.route_name, caller_principal), route.turns_per_hour_override)
    if admission is not AddressAdmission.ADMIT:
        effective_rate = route.turns_per_hour_override or caps.settings.per_address_turns_per_hour
        raise AddressRateLimitedError(
            f"caller {caller_principal!r} is over its rate cap of "
            f"{effective_rate}/hour on route {route.route_name!r}; "
            "retry after a short wait"
        )

    caps.reserve_thread_slot(thread_id)
    intake_token = uuid4().hex
    intake = _new_record(
        route=route,
        message_id=message_id,
        thread_id=thread_id,
        client_address=client_address,
        caller_principal=record_caller_principal,
        provider_message_id=None,
        inbound_text="",
        inbound_kind="event",
        inbound_event=event.model_dump(mode="json"),
        submitted_by=caller_principal,
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    try:
        await store.create_record(intake, intake_token=intake_token)
        await accessors._refresh_thread_mode_ttl(thread_id)
        # The claim is the LAST mutation, mirroring the channel door: an owner id always
        # names a durable record, and a lost claim discards the record just created.
        claimed_owner = await store.claim_event(route_name, event_id, message_id)
    except asyncio.CancelledError:
        caps.release_thread_slot(thread_id)
        _spawn_intake_resolution(message_id)
        raise
    except Exception:
        caps.release_thread_slot(thread_id)
        await _resolve_stranded_intake(message_id)
        raise
    if claimed_owner != message_id:
        caps.release_thread_slot(thread_id)
        await store.delete_record(intake)
        return ApiSubmitResult(message_id=claimed_owner, thread_id=thread_id, answer=None)

    deliver_on_completion = route.door == "channel"
    task = _schedule_turn(
        caps,
        route=route,
        intake=intake,
        text="",
        intake_token=intake_token,
        deliver_on_completion=deliver_on_completion,
        multichannel=multichannel,
    )
    if deliver_on_completion:
        # A channel-target event delivers to the thread's address on completion, exactly as
        # the channel door does; the caller gets the accepted id back.
        return ApiSubmitResult(message_id=message_id, thread_id=thread_id, answer=None)
    wait_seconds = min(submission.wait_seconds, caps.settings.sync_wait_max_seconds)
    return await _api_wait_or_callback(task, message_id, thread_id, wait_seconds)
