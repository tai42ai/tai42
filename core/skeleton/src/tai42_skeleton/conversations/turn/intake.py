"""The channel door ``accept`` and its admission / rate-cap shed path."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from tai42_contract.conversations import BlankInboundTextError, ConversationRoute
from tai42_contract.interactions import LocationElement, MediaItem
from tai42_contract.locale import normalize_optional_locale

from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.caps import AddressAdmission, get_turn_caps
from tai42_skeleton.conversations.delivery import spawn_delivery
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.inbound_checks import (
    _checked_attachments,
    _checked_form,
    _checked_location,
    _checked_params,
)
from tai42_skeleton.conversations.turn.keys import _channel_bucket_key
from tai42_skeleton.conversations.turn.outcome import _text_part
from tai42_skeleton.conversations.turn.record import _new_record, _with_outcome
from tai42_skeleton.conversations.turn.redrive import _resolve_stranded_intake
from tai42_skeleton.conversations.turn.routing import (
    _Multichannel,
    _multichannel_context,
    _resolve_channel_route,
    _resolve_thread_id,
)
from tai42_skeleton.conversations.turn.schedule import _schedule_turn, _spawn_intake_resolution

logger = logging.getLogger("tai42_skeleton.conversations.turn")

# Delivered once per refill window to an address over its rate cap.
_SLOW_DOWN_TEXT = "You are sending messages faster than I can answer. Please wait a moment and try again."


async def accept(
    channel: str,
    our_identity: str,
    client_address: str,
    cap_key: str,
    text: str,
    provider_message_id: str,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
    locale: str | None = None,
) -> str:
    """Accept one inbound channel message, persist-and-deliver its answer, and return its
    ``message_id`` (a uuid4). See :meth:`AppConversations.accept`.

    Idempotent on ``(channel, provider_message_id)``: a redelivery returns the existing
    ``message_id`` and starts no second turn. Every gate that can refuse runs before any
    state is written, so a refusal leaves the pair unclaimed. The turn runs in the
    background; the caller gets the id immediately.

    ``client_address`` is the conversation identity (thread and transcript); ``cap_key``
    is the accountable party the per-address turn cap buckets on, which the door composes.
    Both are canonicalized and a blank ``cap_key`` is refused, so a door that omits the
    accountable key fails loudly rather than silently sharing one bucket.

    Non-empty ``params`` reach a tool target's payload under ``params``; ``None``/empty
    leave the turn byte-identical to today. The door validates their bounds before accept;
    this seam runs only a cheap isinstance sweep against its in-process caller.

    ``form`` is a structured participant submission (an ask-less form's answers) riding WITH the
    required rendered ``text`` — the text stays the turn every consumer sees, while a tool
    route's ``payload_expr`` may map the structured copy from the payload's ``form`` key.
    It is validated here against the contract's transport bounds
    (``validate_inbound_form`` — a JSON object, bounded depth and size, contents opaque and
    untrusted) BEFORE any state is written, and stored on the record's ``inbound_form``
    beside the text (shed records included).

    ``attachments`` (the participant's structured media) and ``location`` (a geographic point the participant
    shared) ride WITH the text the same way — validated here (the shared media list-level caps; a
    ``LocationElement`` is self-validating) BEFORE any state is written, stored on the record's
    ``inbound_attachments``/``inbound_location`` (shed records included), and surfaced to a tool
    target's payload under the stable ``attachments``/``location`` keys only when present.

    A blank/whitespace-only ``text`` is refused with :class:`BlankInboundTextError` before
    any state is written — there is nothing to run a turn on — for the channel adapter to
    drop like an unrouted message."""
    checked_params = _checked_params(params)
    checked_form = _checked_form(form)
    checked_attachments = _checked_attachments(attachments)
    checked_location = _checked_location(location)
    checked_locale = normalize_optional_locale(locale)
    if not text.strip():
        raise BlankInboundTextError(
            f"channel {channel!r} inbound {provider_message_id!r} carries blank text; nothing to run a turn on"
        )
    channel_identity = canonical_address(our_identity)
    address = canonical_address(client_address)
    cap_bucket = canonical_address(cap_key)
    route = await _resolve_channel_route(channel, channel_identity)
    multichannel = await _multichannel_context(
        route,
        door="channel",
        channel=route.channel,
        our_identity=route.our_identity,
        address=address,
        accountable=cap_bucket,
    )
    thread_id = await _resolve_thread_id(route, multichannel, address)
    store = accessors._store()

    owner = await store.get_inbound_owner(channel, provider_message_id)
    if owner is not None:
        # Redelivery of a message already accepted: return the prior turn's id.
        return owner

    message_id = str(uuid4())
    admission = get_turn_caps().admit_address(
        _channel_bucket_key(route.route_name, cap_bucket), route.turns_per_hour_override
    )
    if admission is AddressAdmission.SHED_WITH_REPLY:
        return await _shed_with_reply(
            store,
            route=route,
            channel=channel,
            message_id=message_id,
            thread_id=thread_id,
            client_address=address,
            text=text,
            provider_message_id=provider_message_id,
            form=checked_form,
            attachments=checked_attachments,
            location=checked_location,
        )
    if admission is AddressAdmission.SHED_SILENT:
        return await _shed_silently(
            store,
            route=route,
            channel=channel,
            message_id=message_id,
            thread_id=thread_id,
            client_address=address,
            text=text,
            provider_message_id=provider_message_id,
            form=checked_form,
            attachments=checked_attachments,
            location=checked_location,
        )

    return await _accept_for_turn(
        store,
        route=route,
        channel=channel,
        message_id=message_id,
        thread_id=thread_id,
        client_address=address,
        text=text,
        provider_message_id=provider_message_id,
        multichannel=multichannel,
        params=checked_params,
        form=checked_form,
        attachments=checked_attachments,
        location=checked_location,
        locale=checked_locale,
    )


async def _accept_for_turn(
    store: ConversationRecordStore,
    *,
    route: ConversationRoute,
    channel: str,
    message_id: str,
    thread_id: str,
    client_address: str,
    text: str,
    provider_message_id: str,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
    locale: str | None = None,
) -> str:
    """Commit an admitted channel message to a turn in the one order that keeps the
    release-less inbound claim sound: reserve the per-thread FIFO slot (the last gate that
    can refuse, and it refuses with nothing written), persist the intake record, claim the
    inbound pair, schedule the turn. Losing the claim means a concurrent attempt committed
    first, so this one releases its slot, discards its record and returns the winner's id."""
    caps = get_turn_caps()
    caps.reserve_thread_slot(thread_id)
    intake_token = uuid4().hex
    intake = _new_record(
        route=route,
        message_id=message_id,
        thread_id=thread_id,
        client_address=client_address,
        caller_principal=None,
        provider_message_id=provider_message_id,
        inbound_text=text,
        inbound_form=form,
        inbound_attachments=attachments,
        inbound_location=location,
        inbound_locale=locale,
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    try:
        await store.create_record(intake, intake_token=intake_token)
        await accessors._refresh_thread_mode_ttl(thread_id)
        owner = await store.claim_inbound(channel, provider_message_id, message_id)
    except asyncio.CancelledError:
        # A cancelled task cannot await the round-trips the resolution needs, so it is
        # handed to a fresh task.
        caps.release_thread_slot(thread_id)
        _spawn_intake_resolution(message_id)
        raise
    except Exception:
        # The claim may have been APPLIED with only its reply lost, so the record is
        # resolved against its inbound pair now instead of waiting out its intake lease.
        caps.release_thread_slot(thread_id)
        await _resolve_stranded_intake(message_id)
        raise
    if owner != message_id:
        caps.release_thread_slot(thread_id)
        await store.delete_record(intake)
        return owner

    _schedule_turn(
        caps,
        route=route,
        intake=intake,
        text=text,
        intake_token=intake_token,
        deliver_on_completion=True,
        multichannel=multichannel,
        params=params,
        form=form,
        attachments=attachments,
        location=location,
    )
    return message_id


async def _shed_with_reply(
    store: ConversationRecordStore,
    *,
    route: ConversationRoute,
    channel: str,
    message_id: str,
    thread_id: str,
    client_address: str,
    text: str,
    provider_message_id: str,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> str:
    """Answer an over-limit address with its one paid slow-down reply, committed in the
    turn path's order: the record is persisted at ``accepted`` under an intake lease, the
    inbound pair is claimed, and only then does the guarded transition make it deliverable.
    A record the delivery machine drives must never stand behind an unclaimed pair. No turn
    runs, so no thread slot is reserved."""
    intake_token = uuid4().hex
    intake = _new_record(
        route=route,
        message_id=message_id,
        thread_id=thread_id,
        client_address=client_address,
        caller_principal=None,
        provider_message_id=provider_message_id,
        inbound_text=text,
        inbound_form=form,
        inbound_attachments=attachments,
        inbound_location=location,
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    try:
        await store.create_record(intake, intake_token=intake_token)
        owner = await store.claim_inbound(channel, provider_message_id, message_id)
    except asyncio.CancelledError:
        _spawn_intake_resolution(message_id)
        raise
    except Exception:
        # The claim may have been APPLIED with only its reply lost, so the record is
        # resolved against its inbound pair now instead of waiting out its intake lease.
        await _resolve_stranded_intake(message_id)
        raise
    if owner != message_id:
        await store.delete_record(intake)
        return owner
    completed = _with_outcome(intake, "answered", [_text_part(_SLOW_DOWN_TEXT)], None)
    outcome = await store.complete_turn(completed)
    if outcome != 1:
        raise RuntimeError(
            f"conversations: shed record {message_id} is no longer at intake (complete_turn answered {outcome}); "
            "its outcome was resolved elsewhere and the slow-down reply is discarded"
        )
    spawn_delivery(message_id)
    return message_id


async def _shed_silently(
    store: ConversationRecordStore,
    *,
    route: ConversationRoute,
    channel: str,
    message_id: str,
    thread_id: str,
    client_address: str,
    text: str,
    provider_message_id: str,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> str:
    """Drop a message from an address already given its slow-down reply this window,
    leaving a terminal ``shed`` record. The claim behind that record is what makes a
    provider redelivery resolve to it instead of buying the address another turn."""
    record = _new_record(
        route=route,
        message_id=message_id,
        thread_id=thread_id,
        client_address=client_address,
        caller_principal=None,
        provider_message_id=provider_message_id,
        inbound_text=text,
        inbound_form=form,
        inbound_attachments=attachments,
        inbound_location=location,
        delivery_status=DeliveryStatus.SHED,
        error=f"address {client_address!r} was over its rate cap after a prior slow-down reply",
    )
    await store.create_record(record)
    owner = await store.claim_inbound(channel, provider_message_id, message_id)
    if owner != message_id:
        await store.delete_record(record)
        return owner
    logger.warning(
        "conversations: address %r on route %r is over its rate cap; message dropped after a prior slow-down reply",
        client_address,
        route.route_name,
    )
    return message_id
