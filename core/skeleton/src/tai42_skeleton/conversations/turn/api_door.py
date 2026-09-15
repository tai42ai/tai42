"""The authed API door ``submit_api_message`` and its route lookup."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from tai42_contract.conversations import ConversationRoute
from tai42_contract.interactions import LocationElement, MediaItem
from tai42_contract.locale import normalize_optional_locale

from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.caps import AddressAdmission, AddressRateLimitedError, get_turn_caps
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.api_wait import ApiSubmitResult, _api_wait_or_callback
from tai42_skeleton.conversations.turn.errors import ConversationRouteResolutionError, UnauthenticatedApiCallerError
from tai42_skeleton.conversations.turn.inbound_checks import (
    _checked_attachments,
    _checked_form,
    _checked_location,
    _checked_params,
)
from tai42_skeleton.conversations.turn.keys import _api_bucket_key, _api_client_address
from tai42_skeleton.conversations.turn.record import _new_record
from tai42_skeleton.conversations.turn.routing import _multichannel_context, _resolve_thread_id
from tai42_skeleton.conversations.turn.schedule import _schedule_turn


async def submit_api_message(
    route_name: str,
    external_user_id: str,
    text: str,
    caller_principal: str | None,
    wait_seconds: int,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
    locale: str | None = None,
) -> ApiSubmitResult:
    """Accept one authed API-door message and run its turn.

    ``wait_seconds`` (clamped to ``sync_wait_max_seconds`` by the door, ``0`` for the
    pure-async path) bounds a sync wait: a turn that finishes inside it answers in the
    ``200`` with the callback suppressed, otherwise the door returns ``202`` and the answer
    is POSTed to the callback.

    Admission runs in the channel door's order — rate cap, thread reservation, intake
    record — so a refusal writes nothing and a returned ``message_id`` always names a
    durable record. ``external_user_id`` is matched VERBATIM after a whitespace trim: two
    spellings are two threads.

    ``caller_principal`` is MANDATORY: it qualifies the thread (so no caller can reach
    another's conversation memory by naming its ``external_user_id``) and it alone keys the
    rate bucket (so the cap bounds the accountable party, not a value the caller picks).

    Non-empty ``params`` reach a tool target's payload under ``params``; ``None``/empty
    leave the turn byte-identical to today. The door validates their bounds before submit;
    this seam runs only a cheap isinstance sweep against its in-process caller.

    ``form`` is the structured participant submission riding WITH ``text``
    (``ConversationMessage.form``), carried with the same record + payload semantics the
    channel door has: transport-bounds-checked here (defensively — the door's body model
    already validated it), stored on the record's ``inbound_form``, and surfaced to a tool
    target's payload under ``form`` only when present. ``attachments`` and ``location``
    (``ConversationMessage.attachments``/``.location``) ride WITH ``text`` under the SAME
    record + payload semantics — validated defensively here, stored on the record's
    ``inbound_attachments``/``inbound_location``, and surfaced to a tool target's payload under
    the stable ``attachments``/``location`` keys only when present.
    """
    checked_params = _checked_params(params)
    checked_form = _checked_form(form)
    checked_attachments = _checked_attachments(attachments)
    checked_location = _checked_location(location)
    checked_locale = normalize_optional_locale(locale)
    if caller_principal is None or not caller_principal.strip():
        raise UnauthenticatedApiCallerError(
            f"api conversation route {route_name!r} needs an accountable caller principal and this "
            "deployment resolved none"
        )
    address = canonical_address(external_user_id)
    route = await _get_api_route(route_name)
    client_address = _api_client_address(caller_principal, address)
    multichannel = await _multichannel_context(
        route, door="api", channel=None, our_identity=None, address=client_address, accountable=caller_principal
    )
    thread_id = await _resolve_thread_id(route, multichannel, client_address)
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
        caller_principal=caller_principal,
        provider_message_id=None,
        inbound_text=text,
        inbound_form=checked_form,
        inbound_attachments=checked_attachments,
        inbound_location=checked_location,
        inbound_locale=checked_locale,
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    try:
        await accessors._store().create_record(intake, intake_token=intake_token)
        await accessors._refresh_thread_mode_ttl(thread_id)
    except BaseException:
        caps.release_thread_slot(thread_id)
        raise

    task = _schedule_turn(
        caps,
        route=route,
        intake=intake,
        text=text,
        intake_token=intake_token,
        deliver_on_completion=False,
        multichannel=multichannel,
        params=checked_params,
        form=checked_form,
        attachments=checked_attachments,
        location=checked_location,
    )

    return await _api_wait_or_callback(task, message_id, thread_id, wait_seconds)


async def _get_api_route(route_name: str) -> ConversationRoute:
    route = await cache.get_conversations_manager().get_route(route_name)
    if route is None or route.door != "api":
        raise ConversationRouteResolutionError(f"no api conversation route named {route_name!r}")
    return route
