"""Resumed-answer delivery — the two park-completion tools.

A parked agent turn and a parked tool turn each deliver their deferred outcome back into
the originating thread through a completion continuation bound around the run; these two
tools reverse the thread and mint the resumed answer as a delivered record.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.conversations import AnswerPart, ConversationRoute
from tai42_contract.interactions import PARK_COMPLETION_FAILED, PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX
from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.delivery import spawn_delivery
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.errors import CompletionDeliveryError
from tai42_skeleton.conversations.turn.outcome import _error_answer_text, _serialize_structured, _text_part
from tai42_skeleton.conversations.turn.record import _answer_fields, _new_record
from tai42_skeleton.conversations.turn.tool_turn import _reply_parts, _tool_reply

logger = logging.getLogger("tai42_skeleton.conversations.turn")

# The registered name of the hidden completion-delivery tool. The conversation door binds it
# (``set_park_completion``) around an agent turn, carrying this turn's thread as the opaque
# delivery address, so an async ``ask_user`` may park with a path back to this thread; a resumed
# run's driver fires it with the generic contract payload (that context merged with
# ``{result, completion_id, status}``) and it mints the answered record + spawns delivery. Must
# equal the registered tool name.
COMPLETION_TOOL_NAME = "conversation_deliver"

# The registered name of the hidden GENERIC tool-route completion-delivery tool. The
# conversation door binds it (``set_park_completion``) around a TOOL turn, carrying this
# turn's thread as the opaque delivery address AND the originating route name, so ANY parking
# tool may async-park with a path back to this thread; the parked tool's own resumer fires it
# with the deferred outcome and it maps that through the originating route's ``reply_expr`` +
# spawns delivery. Must equal the registered tool name. Knows nothing of the parking tool —
# only the route contract.
DELIVER_TOOL_COMPLETION_NAME = "deliver_tool_completion"

# Recorded as the sending principal on a completion-delivered record — a namespaced system
# sentinel (no operator answered by hand) that cannot collide with a looked-up user id.
_COMPLETION_PRINCIPAL = "system:agent-resume"


async def _resolve_completion_target(thread_id: str) -> tuple[ConversationRoute, str]:
    """Reverse a parked bridge ``thread_id`` to the ``(route, client_address)`` a resumed
    answer is delivered against.

    A route-keyed thread (``bridge:{route_name}:{address}``) carries both directly. A LINKED
    person's aggregated thread (``bridge:@person:{id}``) has no single address, so the reply
    returns to where they LAST wrote from — the newest record's route + client_address. A
    thread outside the reserved namespace, an unknown/deleted route, or an empty person
    thread raises loudly rather than delivering to a guessed target."""
    if thread_id.startswith(PERSON_THREAD_PREFIX):
        person_id = thread_id[len(PERSON_THREAD_PREFIX) :]
        person = await accessors._person_store().get_by_id(person_id)
        if person is None:
            raise CompletionDeliveryError(f"no person for parked thread {thread_id!r}")
        person_routes = sorted({route for address in person.addresses for route in address.routes})
        newest = await accessors._store().list_person_thread_records(
            person_routes, thread_id, offset=0, limit=1, newest_first=True
        )
        if not newest.records:
            raise CompletionDeliveryError(f"person thread {thread_id!r} holds no record to infer a send target from")
        record = newest.records[0]
        route_name, client_address = record.route_name, record.client_address
    elif thread_id.startswith(BRIDGE_THREAD_PREFIX):
        remainder = thread_id[len(BRIDGE_THREAD_PREFIX) :]
        route_name, _, client_address = remainder.partition(":")
        if not route_name or not client_address:
            raise CompletionDeliveryError(f"parked thread {thread_id!r} is not a route-keyed bridge thread")
    else:
        raise CompletionDeliveryError(f"thread {thread_id!r} is not a reserved bridge thread")
    route = await cache.get_conversations_manager().get_route(route_name)
    if route is None:
        raise CompletionDeliveryError(f"route {route_name!r} for parked thread {thread_id!r} no longer exists")
    return route, client_address


def _completion_succeeded(tool_name: str, completion_id: str, status: str | None) -> bool:
    """Whether a completion fire's ``status`` names the clean-success terminal — and the one
    place a non-success fire is ANNOUNCED.

    The shared contract vocabulary carries exactly two values: :data:`PARK_COMPLETION_SUCCEEDED`
    (deliver the terminal ``result``) and :data:`PARK_COMPLETION_FAILED` (deliver the uniform
    client-safe notice). Every other shape is non-success — an UNSTAMPED fire (``None``: a
    resumer that predates the status field, or one that simply omits it) and an UNRECOGNIZED
    value alike — because pushing an unknown terminal through the success path would deliver a
    failure payload as if it were the answer.

    Those two shapes are a version skew between the resumer and this delivery tool, and the skew
    is otherwise INVISIBLE: the fire still returns a delivered record, so a whole fleet can
    silently degrade every successful answer into an error notice with nothing anywhere to see
    it. This warning is the only detection, so EVERY non-success fire — the explicit failure
    included — names the completion id and WHICH shape arrived: nothing distinguishes a degraded
    fleet's records from a genuinely failing one's, so both have to be visible."""
    if status == PARK_COMPLETION_SUCCEEDED:
        return True
    if status is None:
        reason = "it carried NO status (an unstamped fire — a resumer that predates the status field)"
    elif status == PARK_COMPLETION_FAILED:
        # Phrased about the VALUE, not the sender: a caller whose own default supplies this
        # constant reaches here without having reported anything.
        reason = f"its status is the non-success terminal {PARK_COMPLETION_FAILED!r}"
    else:
        reason = f"it carried the unrecognized status {status!r} (not in the contract vocabulary)"
    logger.warning(
        "conversations: %s (%s) delivers the client-safe notice instead of the result because %s",
        tool_name,
        completion_id,
        reason,
    )
    return False


async def deliver_agent_completion(
    thread_id: str | None = None,
    result: Any = None,
    completion_id: str | None = None,
    status: str | None = None,
) -> dict[str, str | None]:
    """Deliver a resumed agent turn's FINAL answer back into its originating thread.

    The completion continuation the conversation door bound around a parked agent turn: a
    resumed run's driver fires it by name with the generic contract payload — the bound context
    (``thread_id``, this turn's delivery address) merged with ``{result, completion_id, status}``
    — when the run drives to a terminal out of band. It reverses the thread to its route +
    address, mints the outcome as an already-``answered`` record keyed by ``completion_id``, and
    hands it to the SAME delivery machine a produced answer takes — never appending to the
    agent's memory (the resumed run already recorded the answer in its own checkpoint).

    ``status`` names the terminal outcome with the shared contract vocabulary, exactly as the
    tool-route sibling :func:`deliver_tool_completion` reads it: :data:`PARK_COMPLETION_SUCCEEDED`
    delivers ``result`` as the answer; every other shape — the explicit
    :data:`PARK_COMPLETION_FAILED`, an UNSTAMPED fire (``None``), and an UNRECOGNIZED value
    alike — is a non-success terminal (an agent route carries no error mapping) delivered as the
    uniform client-safe notice, so a failed/stopped/aborted resume is never silently dropped and
    a fire this tool cannot read never delivers a non-success payload as if it were the answer.
    Both fail-safe shapes are a resumer/delivery version skew, so each is warned about by
    :func:`_completion_succeeded` — that log is their only detection.

    ``completion_id`` is the stable idempotency id of the resolved super-step: the delivery
    record is keyed by it, so a lease-lapse re-drive that fires the completion a second time
    with the same id finds the record already committed and is a benign no-op — the durable
    record commit is the exactly-once point (as far as the store makes it: the record write is a
    best-effort HSET, so this read-then-write is the caller-side dedupe, not a store-enforced
    uniqueness constraint — two fires racing the same id can both pass the read). A blank
    successful answer — or a success fire carrying NO ``result`` at all, which would otherwise
    serialize to the literal ``"null"`` — delivers the SAME client-safe error reply the fresh-turn
    path delivers for an empty answer, so it is not a silent non-delivery. Returns
    ``{"message_id": completion_id}`` for the delivered (or already delivered) record. An
    unresolvable thread raises :class:`CompletionDeliveryError` loudly.

    ``thread_id`` is defaulted only so a fire carrying NO delivery address is handled rather
    than crashing the resumer: nothing reverses a completion id to a thread, so such a fire is a
    LOGGED no-op returning ``{"message_id": None}``. Every binding this module makes carries the
    address.

    The two missing halves are answered differently ON PURPOSE. A missing ``completion_id``
    RAISES: it is the idempotency key and nothing can guess a stable one, so delivering anyway
    would risk a duplicate reply on every retry — worse for the person reading the thread than a
    bounded retry of a malformed fire. A missing ``thread_id`` DROPS: the payload is permanently
    unroutable, so raising would only buy an unending retry storm against a fire no attempt can
    ever land."""
    if not completion_id:
        raise ValueError("deliver_agent_completion requires a completion_id — it is the exactly-once delivery key")
    existing = await accessors._store().get_record(completion_id)
    if existing is not None:
        # A redelivered completion for a super-step whose durable record already committed (a
        # lease-lapse re-drive): the exactly-once point is passed, so this is a benign no-op.
        # Read BEFORE the address guard, so a re-drive of an already-delivered super-step takes
        # this benign path even when the second fire lost its address.
        return {"message_id": completion_id}
    if not thread_id:
        # A fire whose completion binding carried no address: nothing can route it and no retry
        # ever will, so it is dropped LOUDLY rather than raising the retriable delivery error.
        logger.error(
            "conversations: an agent park completion (%s) fired with no bound thread_id; it carries no delivery "
            "address, so the resumed answer cannot be routed and is dropped",
            completion_id,
        )
        return {"message_id": None}
    route, client_address = await _resolve_completion_target(thread_id)
    if _completion_succeeded(COMPLETION_TOOL_NAME, completion_id, status):
        # A blank resumed answer delivers the SAME client-safe error text the fresh-turn path
        # replies for an empty answer, so the client sees a consistent outcome either way — the
        # route's own ``error_reply_text`` when it carries one. It rides the operator record as
        # an ``answered`` reply (an operator record is always answered) — the text is the reply,
        # there is no client turn to mark ``error`` against.
        if result is None:
            # A success fire carrying NO result: serializing it would render the literal "null",
            # which is not blank and would sail past the check below straight into the participant's
            # thread. It is the same malformed-payload class the status guards catch, so it takes
            # the same client-safe notice and is announced for the same reason.
            logger.warning(
                "conversations: %s (%s) reported success with no result; delivering the client-safe notice "
                "instead of a serialized null",
                COMPLETION_TOOL_NAME,
                completion_id,
            )
            answer = _error_answer_text(route)
        else:
            text = _serialize_structured(result)
            answer = text if text.strip() else _error_answer_text(route)
    else:
        # A non-success terminal: an agent route carries no error mapping, so deliver the uniform
        # client-safe notice — never the raw terminal detail, never silence.
        answer = _error_answer_text(route)
    record = _new_record(
        route=route,
        message_id=completion_id,
        thread_id=thread_id,
        client_address=client_address,
        caller_principal=_COMPLETION_PRINCIPAL,
        provider_message_id=None,
        inbound_text="",
        delivery_status=DeliveryStatus.PENDING_DELIVERY,
        answer_status="answered",
        answer=answer,
        origin="operator",
    )
    await accessors._store().create_record(record)
    await accessors._refresh_thread_mode_ttl(thread_id)
    spawn_delivery(completion_id)
    return {"message_id": completion_id}


async def deliver_tool_completion(
    delivery_thread_id: str | None,
    completion_id: str | None,
    result: Any = None,
    status: str | None = PARK_COMPLETION_FAILED,
    route_name: str | None = None,
) -> dict[str, str | None]:
    """Deliver a resumed TOOL route's deferred outcome back into its originating thread.

    The GENERIC sibling of :func:`deliver_agent_completion`: the completion continuation the
    conversation door binds around a parked tool turn. When the parked tool's own resumer
    drives to a clean terminal out of band, it fires this by name with the bound context
    (``delivery_thread_id`` plus the originating ``route_name``) plus
    ``{completion_id, result, status}``. It reverses the thread to its delivery address (the
    SAME reversal an agent completion uses — a tool turn runs under the same reversible bridge
    thread), maps the terminal ``result`` through the ORIGINATING route's ``reply_expr`` (the
    SAME mapping the live tool turn applied), and hands the reply to the SAME delivery machine a
    produced answer takes.

    ``status`` names the terminal outcome with the shared contract vocabulary:
    :data:`PARK_COMPLETION_SUCCEEDED` maps ``result`` via ``reply_expr``; every other shape —
    the explicit :data:`PARK_COMPLETION_FAILED`, an UNSTAMPED fire (``None``), and an
    UNRECOGNIZED value alike — is a non-success terminal (the route carries no error mapping)
    delivered as the uniform client-safe notice, so a failed/stopped/aborted resume is never
    silently dropped and a fire this tool cannot read never pushes a non-success payload through
    ``reply_expr``. EVERY non-success shape is warned about by :func:`_completion_succeeded` —
    that log is the only detection a resumer/delivery version skew has, since a degraded fire
    still returns a delivered record. An OMITTED ``status`` keeps this tool's published
    fail-safe default (:data:`PARK_COMPLETION_FAILED`), so it is reported as that terminal
    rather than as an unstamped fire; an explicit ``None`` on the wire is read as unstamped. A
    ``reply_expr`` the terminal cannot be mapped through delivers that SAME client-safe notice
    rather than crashing the resumer. A success whose reply maps to null/blank is a designed
    silent outcome: nothing is delivered and — being naturally idempotent (a redelivered fire
    re-maps the same result to the same null) — it anchors no record.

    ``route_name`` pins the ORIGINATING route the park started under, bound at park time. The
    reply is mapped through THAT route's ``reply_expr`` — not the route the thread's newest
    record happens to name. A linked multichannel person may write from a DIFFERENT route
    between the park and the resume, and its ``reply_expr`` (or absence of one) would map the
    tool's result wrongly; the pinned route keeps the mapping the one the parking turn owned.
    Delivery still lands where the person last wrote (the reversed thread's address), so a
    channel-hopping person still receives the reply. ``None`` (no pin) maps through the
    reversed delivery route, matching a route-keyed thread where the two are the same route.

    ``completion_id`` is the stable idempotency id of the resolved terminal: the delivery
    record is keyed by it, so a redelivered fire finds the record already committed and is a
    benign no-op (as far as the store makes it — the record write is a best-effort HSET, so this
    read-then-write is the caller-side dedupe, not a store-enforced uniqueness constraint).
    Returns ``{"message_id": completion_id}`` for the delivered (or already delivered) record, or
    ``{"message_id": None}`` for a silent outcome. Generic: it knows nothing of the parking tool,
    only the route contract and the opaque context it reverses. A vanished originating route
    raises :class:`CompletionDeliveryError` loudly so the resumer's at-least-once seam retains and
    retries rather than dropping the outcome.

    The two halves of the fire's address are guarded differently ON PURPOSE — the same doctrine
    :func:`deliver_agent_completion` states. A missing ``completion_id`` RAISES: it is the
    idempotency key and nothing can guess a stable one, so delivering anyway would key every
    key-less fire onto ONE record — the second such terminal, from an unrelated park, would read
    the first as already delivered and vanish. A missing ``delivery_thread_id`` DROPS (logged,
    ``{"message_id": None}``): nothing reverses a completion id to a thread, so the payload is
    permanently unroutable and raising the retriable error would only buy an unending retry storm
    against a fire no attempt can ever land. An unresolvable but PRESENT thread still raises —
    that one can come back (a route can be restored), so it is the resumer's to retry."""
    if not completion_id:
        raise ValueError("deliver_tool_completion requires a completion_id — it is the exactly-once delivery key")
    existing = await accessors._store().get_record(completion_id)
    if existing is not None:
        # A redelivered completion for a terminal whose durable record already committed: the
        # exactly-once point is passed, so this is a benign no-op. Read BEFORE the address guard,
        # so a re-fire of an already-delivered terminal takes this benign path even when the
        # second fire lost its address.
        return {"message_id": completion_id}
    if not delivery_thread_id:
        # A fire whose completion binding carried no address: nothing can route it and no retry
        # ever will, so it is dropped LOUDLY rather than raising the retriable delivery error.
        logger.error(
            "conversations: a tool park completion (%s) fired with no bound delivery_thread_id; it carries no "
            "delivery address, so the resumed outcome cannot be routed and is dropped",
            completion_id,
        )
        return {"message_id": None}
    route, client_address = await _resolve_completion_target(delivery_thread_id)
    # Map through the ORIGINATING route's reply_expr (pinned at park time), not the reversed
    # delivery route — a linked person may have written from a different route since the park,
    # whose reply_expr would map the terminal wrongly.
    mapping_route = route
    if route_name is not None:
        pinned = await cache.get_conversations_manager().get_route(route_name)
        if pinned is None:
            raise CompletionDeliveryError(
                f"originating route {route_name!r} for parked thread {delivery_thread_id!r} no longer exists"
            )
        mapping_route = pinned
    if _completion_succeeded(DELIVER_TOOL_COMPLETION_NAME, completion_id, status):
        try:
            reply = await _tool_reply(mapping_route, result)
        except Exception as exc:
            # A terminal the route's reply_expr cannot map is delivered as the client-safe
            # notice rather than crashing the resumer or dropping the outcome.
            logger.error(
                "conversations: mapping a resumed tool result for route %r failed",
                mapping_route.route_name,
                exc_info=exc,
            )
            # The participant-facing notice resolves through the DELIVERY route (where the record is
            # filed and the participant is conversing), so its own ``error_reply_text`` applies.
            parts: list[AnswerPart] | None = [_text_part(_error_answer_text(route))]
        else:
            # A resumed tool reply carries the same ordered-parts shape a live tool turn does
            # (null/blank → silent, a string → one message, an array → ordered messages).
            parts = _reply_parts(reply)
            if parts is None:
                # A designed silent outcome delivers nothing; it re-maps to the same null on a
                # redelivery, so it needs no idempotency record.
                return {"message_id": None}
    else:
        # A non-success terminal: the route carries no error mapping, so deliver the uniform
        # client-safe notice (the delivery route's ``error_reply_text`` when set) — never the
        # raw internal detail, never silence.
        parts = [_text_part(_error_answer_text(route))]
    answer, answer_parts = _answer_fields(parts)
    record = _new_record(
        route=route,
        message_id=completion_id,
        thread_id=delivery_thread_id,
        client_address=client_address,
        caller_principal=_COMPLETION_PRINCIPAL,
        provider_message_id=None,
        inbound_text="",
        delivery_status=DeliveryStatus.PENDING_DELIVERY,
        answer_status="answered",
        answer=answer,
        answer_parts=answer_parts,
        origin="operator",
    )
    await accessors._store().create_record(record)
    await accessors._refresh_thread_mode_ttl(delivery_thread_id)
    spawn_delivery(completion_id)
    return {"message_id": completion_id}
