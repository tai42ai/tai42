"""The turn engine — turns an accepted message into an agent turn.

One inbound message (channel door :func:`accept`, authed API door
:func:`submit_api_message`) resolves to its route, runs that route's agent IN-PROCESS under
the route's execution key, and persists the produced answer as a durable record the delivery
executor sends back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import string
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from tai42_contract.agent import Agent
from tai42_contract.agent.events import InterruptFinal, MessageFinal, StructuredFinal
from tai42_contract.conversations import (
    GREETING_PLACEHOLDER,
    AnswerStatus,
    BlankInboundTextError,
    ConversationAnswer,
    ConversationDoor,
    ConversationRoute,
    CrossTargetMergeError,
    NotLinkedError,
    PairCodeInvalidError,
    Person,
    PersonAddress,
)
from tai42_kit.utils.data import run_jq_bounded

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX
from tai42_skeleton.authz.execution import authorize_execution_agent_run, bind_execution_identity
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.caps import AddressAdmission, AddressRateLimitedError, TurnCaps, get_turn_caps
from tai42_skeleton.conversations.delivery import mark_wait_delivered, spawn_delivery
from tai42_skeleton.conversations.mode import ConversationModeStore, effective_mode, supports_thread_append
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.pair_codes import ConversationPairCodeStore, MintingConversation
from tai42_skeleton.conversations.pairing import Link, Passthrough, Redeem, Unlink, classify
from tai42_skeleton.conversations.persons import ConversationPersonStore, PairingTarget
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.redeem_throttle import ConversationRedeemThrottle
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore
from tai42_skeleton.conversations.turn_context import BridgeTurnContext, bridge_turn_context
from tai42_skeleton.operations.errors import NotSupportedError, PermissionDenied

logger = logging.getLogger(__name__)

# Strong references to in-flight turn tasks so one is not GC'd before it persists.
_TURN_TASKS: set[asyncio.Task] = set()

# Client-safe text for a failed turn; the internal detail goes to the record's ``error``.
_ERROR_ANSWER_TEXT = "Sorry, something went wrong handling your message. Please try again."

# Delivered once per refill window to an address over its rate cap.
_SLOW_DOWN_TEXT = "You are sending messages faster than I can answer. Please wait a moment and try again."

# Fixed, generic pairing-turn replies — no channel names, no links (operators who want
# richer wording compose it from the pairing tool + their own flow).
_LINKED_TEXT = "Done — this conversation is now linked to your other one."
_UNLINKED_TEXT = "Done — this conversation is no longer linked."
_NOT_LINKED_TEXT = "This conversation is not linked to anything, so there is nothing to unlink."
# The UNIFORM redeem refusal: an unknown/expired/already-redeemed code, a cross-target code,
# or a throttled attempt all read the same, so the reply reveals no oracle.
_INVALID_CODE_TEXT = "That pairing code is not valid. It may have expired or already been used."


def _checked_params(params: dict[str, str] | None) -> dict[str, str] | None:
    """Refuse a non-dict or non-str-valued ``params`` loudly, keeping garbage out of the tool
    payload. The doors validate the bounds (``validate_entry_params``); accept trusts its
    in-process caller and runs only this cheap isinstance sweep. ``None`` passes through."""
    if params is None:
        return None
    if not isinstance(params, dict):
        raise ValueError(f"params must be a dict or None, got {type(params).__name__}")
    for key, value in params.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("params must map str keys to str values")
    return params


class ConversationRouteResolutionError(LookupError):
    """No route matches the inbound message, so it is refused rather than dropped."""


class UnauthenticatedApiCallerError(NotSupportedError):
    """The API door was reached with no authenticated caller principal. The turn is refused:
    every thread and rate bucket on that door is keyed by its caller, and an anonymous one
    would be shared by everybody."""


@dataclass(frozen=True)
class ApiSubmitResult:
    """The outcome the API door turns into its HTTP response. ``answer`` is set only when
    the bounded sync-wait finished the turn in time (→ ``200``); otherwise the turn is
    still running behind the callback (→ ``202``)."""

    message_id: str
    thread_id: str
    answer: ConversationAnswer | None


def _thread_id(route_name: str, client_address: str) -> str:
    return f"{BRIDGE_THREAD_PREFIX}{route_name}:{client_address}"


def _api_client_address(caller_principal: str, address: str) -> str:
    """The API door's address slot: its authenticated caller joined to the caller-supplied
    end-user id. The principal is percent-encoded, so it holds no ``/`` and the join is
    unambiguous for any pair; two callers naming one end user get two addresses."""
    return f"{quote(caller_principal, safe='')}/{address}"


def _channel_bucket_key(route_name: str, cap_key: str) -> str:
    """The channel door's rate-bucket key. ``cap_key`` is the party the door named as
    accountable — a provider-attested address, or a self-minting door's network client
    bucket — and the route scopes it so two routes never share a budget."""
    return f"{route_name}|{cap_key}"


def _api_bucket_key(route_name: str, caller_principal: str) -> str:
    """The API door's rate-bucket key. It is the authenticated CALLER, not the composed
    address, whose cardinality the caller still chooses."""
    return f"{route_name}|caller:{caller_principal}"


def _throttle_source_key(door: ConversationDoor, accountable: str) -> str:
    """The redeem-throttle SOURCE scope: the DOOR-QUALIFIED accountable party, never the
    conversation address (whose cardinality the caller freely chooses). Same accountability
    model the rate caps use — the api door keys on its authenticated ``caller_principal``
    (:func:`_api_bucket_key`), the channel door on the provider-attested ``cap_key``
    (:func:`_channel_bucket_key``). A deterministic JSON array (no delimiter joining) whose
    leading door element keeps an api principal string and a channel value from ever
    colliding, and whose accountable part an attacker cannot rotate per attempt — so the
    lock actually arms against a brute-force run."""
    return json.dumps([door, accountable], separators=(",", ":"))


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _person_store() -> ConversationPersonStore:
    return ConversationPersonStore(ConversationsSettings())


def _pair_code_store() -> ConversationPairCodeStore:
    return ConversationPairCodeStore(ConversationsSettings())


def _config_store() -> ConversationTargetConfigStore:
    return ConversationTargetConfigStore(ConversationsSettings())


def _redeem_throttle() -> ConversationRedeemThrottle:
    return ConversationRedeemThrottle(ConversationsSettings())


def _person_thread_id(person_id: str) -> str:
    return f"{PERSON_THREAD_PREFIX}{person_id}"


@dataclass(frozen=True)
class _Multichannel:
    """The per-accept multichannel context for a target with ``multichannel: true`` — the
    target, the sending address in the door's own terms, the accountable party the redeem
    throttle keys on, and the first-contact greeting template. ``None`` everywhere
    multichannel is OFF, which is what keeps an unconfigured or unlinked conversation
    byte-identical to today.

    ``address`` is the PERSON IDENTITY (the thread, the transcript, the pair-code's stored
    conversation); ``accountable`` is the rotation-resistant party the brute-force throttle
    scopes to — the api ``caller_principal`` or the channel ``cap_key`` — and is NEVER the
    conversation address."""

    target: PairingTarget
    door: ConversationDoor
    channel: str | None
    our_identity: str | None
    address: str
    accountable: str
    route_name: str
    greeting_template: str | None

    def address_row(self) -> PersonAddress:
        """This sending address as a fresh :class:`PersonAddress` row (its route attributed)."""
        return PersonAddress(
            door=self.door,
            routes=[self.route_name],
            channel=self.channel,
            our_identity=self.our_identity,
            address=self.address,
            linked_at=datetime.now(UTC),
        )

    def minting_conversation(self) -> MintingConversation:
        """This conversation as the value a minted pair code stores, so the redeem side can
        rebuild a complete address for it."""
        return MintingConversation(
            target_kind=self.target.target_kind,
            target_name=self.target.target_name,
            route_name=self.route_name,
            door=self.door,
            channel=self.channel,
            our_identity=self.our_identity,
            address=self.address,
        )

    def throttle_source_key(self) -> str:
        """The redeem-throttle source key: this accept's DOOR-QUALIFIED accountable party (the
        api ``caller_principal`` or the channel ``cap_key``), NOT the conversation address —
        so an attacker cannot rotate a caller-composed address to dodge the lock."""
        return _throttle_source_key(self.door, self.accountable)


async def _multichannel_context(
    route: ConversationRoute,
    *,
    door: ConversationDoor,
    channel: str | None,
    our_identity: str | None,
    address: str,
    accountable: str,
) -> _Multichannel | None:
    """The multichannel context for ``route``, or ``None`` when the target has no config row
    or its ``multichannel`` is off (default-false). Read once per accept, BEFORE the gates:
    it decides the thread key and, later, the pairing turn and greeting.

    ``address`` is the conversation identity; ``accountable`` is the rotation-resistant party
    the redeem throttle scopes to (the caller of the door, the same key its rate cap uses)."""
    config = await _config_store().get(route.target_kind, route.target_name)
    if config is None or not config.multichannel:
        return None
    return _Multichannel(
        target=PairingTarget(target_kind=route.target_kind, target_name=route.target_name),
        door=door,
        channel=channel,
        our_identity=our_identity,
        address=address,
        accountable=accountable,
        route_name=route.route_name,
        greeting_template=config.greeting_template,
    )


async def _resolve_thread_id(route: ConversationRoute, multichannel: _Multichannel | None, address: str) -> str:
    """The thread key for this accept. A LINKED person on any multichannel target keys the
    aggregated ``bridge:@person:{person_id}`` thread; everyone else keeps today's
    route-keyed ``bridge:{route}:{address}``. Read-only: no person row is created here,
    so a redelivered, refused or shed message never mints identity.

    In-flight merge race: a turn admitted under the old key while the merge lands completes
    under that key (its FIFO slot lives there); the NEXT message keys to the person thread.
    Histories are never migrated (linked memory starts at the pairing moment)."""
    if multichannel is not None:
        person = await _person_store().get_person(
            multichannel.target,
            door=multichannel.door,
            channel=multichannel.channel,
            our_identity=multichannel.our_identity,
            address=multichannel.address,
        )
        if person is not None and len(person.addresses) > 1:
            return _person_thread_id(person.person_id)
    return _thread_id(route.route_name, address)


# -- route resolution --------------------------------------------------------


async def _resolve_channel_route(channel: str, our_identity_canonical: str) -> ConversationRoute:
    """The single ``door=channel`` route matching ``(channel, our_identity)`` by EXACT
    equality on the canonical address form. No match raises
    :class:`ConversationRouteResolutionError`; more than one is a corrupt table and raises
    rather than picking one."""
    routes = await get_conversations_manager().list_routes()
    matches = [
        route
        for route in routes.values()
        if route.door == "channel"
        and route.channel == channel
        and route.our_identity is not None
        and canonical_address(route.our_identity) == our_identity_canonical
    ]
    if not matches:
        raise ConversationRouteResolutionError(
            f"no channel route bound to channel {channel!r} identity {our_identity_canonical!r}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"conversations: {len(matches)} channel routes claim channel {channel!r} identity "
            f"{our_identity_canonical!r}; the routing table is inconsistent"
        )
    return matches[0]


# -- the turn ----------------------------------------------------------------


def _serialize_structured(data: object) -> str:
    if isinstance(data, str):
        return data
    model_dump_json = getattr(data, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json())
    return json.dumps(data, default=str)


async def _drain_answer(agent: Agent, text: str, thread_id: str) -> str:
    """Run the agent to its terminal event and return the answer text. A structured final
    is serialized; an interrupt is not answerable by a background turn and is raised."""
    structured: StructuredFinal | None = None
    message: MessageFinal | None = None
    async for event in agent.astream(user_message=text, thread_id=thread_id):
        if isinstance(event, InterruptFinal):
            raise RuntimeError(f"agent raised an interrupt ({event.interrupt_id}) a background turn cannot answer")
        if isinstance(event, StructuredFinal):
            structured = event
        elif isinstance(event, MessageFinal):
            message = event
    if structured is not None:
        return _serialize_structured(structured.data)
    if message is not None:
        return message.text
    return ""


async def _run_agent_turn(
    route: ConversationRoute, text: str, thread_id: str, client_address: str
) -> tuple[Literal["answered", "error"], str, str | None]:
    """Run one agent turn as the route's execution key and return ``(answer_status, answer,
    error_detail)``. The identity is bound for the turn's duration and the run authorized
    against it before the agent runs. A denied run, a mid-turn error or an empty answer
    becomes a client-safe ``error`` outcome; its detail is returned, never delivered.

    The turn-scoped bridge context is established around the agent invocation, so an
    in-process builtin the agent calls (``set_conversation_mode``) reads the CURRENT
    conversation's thread from it — the same contextvar propagation the bound execution
    identity relies on."""
    agent = _agent_registry().get(route.target_name)
    if agent is None:
        return ("error", _ERROR_ANSWER_TEXT, f"agent {route.target_name!r} is not registered")
    turn_context = BridgeTurnContext(
        thread_id=thread_id,
        route_name=route.route_name,
        channel=route.channel,
        our_identity=route.our_identity,
        client_address=client_address,
    )
    try:
        with bridge_turn_context(turn_context):
            async with bind_execution_identity(
                route.execution_key, bound_fingerprint=route.execution_key_fingerprint
            ) as identity:
                await authorize_execution_agent_run(identity, route.target_name)
                answer = await _drain_answer(agent, text, thread_id)
    except PermissionDenied as exc:
        return ("error", _ERROR_ANSWER_TEXT, f"turn denied: {exc}")
    except Exception as exc:
        # A failed turn becomes a logged error OUTCOME, not a swallowed error.
        logger.error("conversations: turn for route %r failed", route.route_name, exc_info=exc)
        return ("error", _ERROR_ANSWER_TEXT, f"turn error: {exc}")
    if not answer.strip():
        return ("error", _ERROR_ANSWER_TEXT, "agent produced an empty answer")
    return ("answered", answer, None)


def _agent_registry() -> dict[str, Agent]:
    from tai42_skeleton.app import instance

    return instance.app.agents.all_agents()


async def _refresh_thread_mode_ttl(thread_id: str) -> None:
    """Extend a live mode override's retention window on new thread activity, so an override
    lives exactly as long as the conversation stays within its retention window. A no-op when
    none is set — the override is never resurrected."""
    await ConversationModeStore(ConversationsSettings()).refresh_ttl(thread_id)


# -- the tool target ---------------------------------------------------------


@dataclass(frozen=True)
class _SilentOutcome:
    """A tool turn that produced no reply — a designed no-reply, never an error. On the
    channel door nothing is ever sent (terminal ``silent``); on the api door an explicit
    silent marker is delivered through the durable machine."""


@dataclass(frozen=True)
class _ResolvedOutcome:
    """A tool turn that produced an outcome to deliver: an ``answered`` reply or a
    client-safe ``error``. Both carry non-blank ``answer`` text — the fields are
    non-optional, so an outcome missing its answer cannot be represented."""

    answer_status: Literal["answered", "error"]
    answer: str
    error: str | None


#: A tool turn resolves to exactly one of these two shapes — no third, coercible state.
_ToolOutcome = _SilentOutcome | _ResolvedOutcome

#: A freshly minted pair code and its expiry, as :meth:`ConversationPairCodeStore.mint` returns.
_MintedCode = tuple[str, datetime]


def _tool_error(detail: str) -> _ResolvedOutcome:
    return _ResolvedOutcome(answer_status="error", answer=_ERROR_ANSWER_TEXT, error=detail)


async def _run_tool_turn(
    route: ConversationRoute,
    text: str,
    client_address: str,
    person: Person | None = None,
    params: dict[str, str] | None = None,
) -> _ToolOutcome:
    """Dispatch one tool turn as the route's execution key and return its resolved outcome
    (a :class:`_SilentOutcome` or a :class:`_ResolvedOutcome`).

    Stateless per message — no conversation memory. The inbound payload maps to
    the tool kwargs (``payload_expr`` or a fixed ``{message, sender}``), the tool runs under
    the bound execution identity (whose ``run_tool`` seam authorizes the dispatch), and the
    result maps to the reply (``reply_expr`` or a null/string pass-through). The payload
    carries ``person_id`` and ``person_addresses`` IFF the target has multichannel on;
    ``sender`` stays the sending address either way. Non-empty ``params`` nest under a
    ``params`` key (never merged into the root); ``None``/empty leave the payload unchanged.
    A reply that is ``None`` or blank is a deliberate silent outcome; a mapping fault, a
    denied or failed dispatch, or a wrong-typed result is a client-safe ``error`` outcome
    whose detail is logged, never delivered."""
    payload: dict[str, object] = {
        "message": text,
        "sender": client_address,
        "our_identity": route.our_identity,
        "channel": route.channel,
    }
    if person is not None:
        payload["person_id"] = person.person_id
        payload["person_addresses"] = [a.model_dump(mode="json") for a in person.addresses]
    if params:
        payload["params"] = params
    try:
        kwargs = await _tool_kwargs(route, payload)
    except Exception as exc:
        # VALUE-FREE: a jq runtime error embeds the offending payload input (which now
        # carries opaque entry params) in its text, and this detail is both logged and
        # persisted into the record — so it names the error CLASS only, never its message
        # and never ``exc_info``. The adjacent tool-run/reply-mapping paths keep their
        # diagnosable text: they render the tool's own output, not the platform's jq input.
        logger.error(
            "conversations: mapping the inbound payload for route %r failed with %s",
            route.route_name,
            type(exc).__name__,
        )
        return _tool_error(f"payload_expr error ({type(exc).__name__})")
    try:
        async with bind_execution_identity(route.execution_key, bound_fingerprint=route.execution_key_fingerprint):
            # ``offload_sync``: a synchronous tool runs off the event loop, matching the
            # meta-executor door, so a blocking tool cannot starve the turn engine.
            result = await _tools().run_tool(route.target_name, kwargs, offload_sync=True)
    except PermissionDenied as exc:
        return _tool_error(f"turn denied: {exc}")
    except Exception as exc:
        logger.error("conversations: tool turn for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"turn error: {exc}")
    try:
        reply = await _tool_reply(route, result)
    except Exception as exc:
        logger.error("conversations: mapping the tool result for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"reply_expr error: {exc}")
    if reply is None or not reply.strip():
        return _SilentOutcome()
    return _ResolvedOutcome(answer_status="answered", answer=reply, error=None)


async def _tool_kwargs(route: ConversationRoute, payload: dict[str, object]) -> dict[str, object]:
    """The kwargs the tool is dispatched with. No ``payload_expr`` → the fixed
    ``{message, sender}``. Otherwise the jq program over the full payload, which MUST emit
    exactly one value and it MUST be a JSON object."""
    if route.payload_expr is None:
        return {"message": payload["message"], "sender": payload["sender"]}
    # Bounded at one, so an over-emitting program is capped rather than materialized whole.
    values = await run_jq_bounded(route.payload_expr, payload, 1)
    if len(values) != 1:
        raise ValueError(f"payload_expr must emit exactly one value, emitted {'more than one' if values else 'none'}")
    kwargs = values[0]
    if not isinstance(kwargs, dict):
        raise ValueError(f"payload_expr must emit a JSON object, emitted {type(kwargs).__name__}")
    return kwargs


async def _tool_reply(route: ConversationRoute, result: object) -> str | None:
    """The reply text the tool result maps to, or ``None`` for a silent outcome. No
    ``reply_expr`` → the result must itself be ``None`` or a string. Otherwise the jq
    program over the raw result, which MUST emit exactly one value and it MUST be null or a
    string."""
    if route.reply_expr is None:
        if result is None or isinstance(result, str):
            return result
        raise ValueError(
            f"a tool target with no reply_expr must return null or a string, returned {type(result).__name__}"
        )
    # Bounded at one, so an over-emitting program is capped rather than materialized whole.
    values = await run_jq_bounded(route.reply_expr, result, 1)
    if len(values) != 1:
        raise ValueError(f"reply_expr must emit exactly one value, emitted {'more than one' if values else 'none'}")
    reply = values[0]
    if reply is not None and not isinstance(reply, str):
        raise ValueError(f"reply_expr must emit null or a string, emitted {type(reply).__name__}")
    return reply


def _tools():
    from tai42_skeleton.app import instance

    return instance.app.tools


def _new_record(
    *,
    route: ConversationRoute,
    message_id: str,
    thread_id: str,
    client_address: str,
    caller_principal: str | None,
    provider_message_id: str | None,
    delivery_status: DeliveryStatus,
    inbound_text: str,
    answer_status: AnswerStatus | None = None,
    answer: str | None = None,
    error: str | None = None,
    origin: Literal["client", "operator"] = "client",
) -> ConversationRecord:
    """A freshly minted record for one accepted message, in the state its door commits it
    to (``accepted``, ``pending_delivery`` or ``shed``). ``inbound_text`` is the message
    verbatim, durable from here so the record reads as a turn of a conversation and not
    only as its answer. A ``client`` api-door record MUST name the authenticated caller its
    thread and rate bucket are keyed by, and a ``client`` channel-door record names none; an
    ``operator`` record names the operator that sent it on EITHER door — it rides no rate
    bucket and carries no inbound to dedupe."""
    if origin == "operator":
        if not (caller_principal and caller_principal.strip()):
            raise RuntimeError(
                f"conversations: an operator record on route {route.route_name!r} requires the sending "
                "operator principal in caller_principal"
            )
    elif (route.door == "api") != bool(caller_principal and caller_principal.strip()):
        raise RuntimeError(
            f"conversations: a {route.door} record cannot carry caller_principal={caller_principal!r}; "
            "the api door requires one and the channel door has none"
        )
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name=route.route_name,
        door=route.door,
        thread_id=thread_id,
        client_address=client_address,
        channel=route.channel,
        our_identity=route.our_identity,
        callback_url=route.callback_url,
        caller_principal=caller_principal,
        origin=origin,
        provider_message_id=provider_message_id,
        inbound_text=inbound_text,
        delivery_status=delivery_status,
        answer_status=answer_status,
        answer=answer,
        error=error,
        created_at=now,
        updated_at=now,
    )


def _with_outcome(
    intake: ConversationRecord, answer_status: AnswerStatus, answer: str, error_detail: str | None
) -> ConversationRecord:
    """``intake`` carrying a produced outcome and moved to ``pending_delivery`` — the shape
    :meth:`ConversationRecordStore.complete_turn` requires."""
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": answer_status,
            "answer": answer,
            "error": error_detail,
            "delivery_status": DeliveryStatus.PENDING_DELIVERY,
            "updated_at": time.time(),
        }
    )


def _with_channel_silent(intake: ConversationRecord) -> ConversationRecord:
    """``intake`` moved to terminal ``silent`` — a CHANNEL-door tool turn that produced no
    reply, so nothing is ever sent. Carries no answer_status, matching
    :data:`ANSWERLESS_STATUSES`."""
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": None,
            "answer": None,
            "error": None,
            "delivery_status": DeliveryStatus.SILENT,
            "updated_at": time.time(),
        }
    )


def _with_api_silent(intake: ConversationRecord) -> ConversationRecord:
    """``intake`` moved to ``pending_delivery`` carrying a ``silent`` outcome — an API-door
    tool turn that produced no reply. The api door answered ``202`` promising a callback, so
    the silent outcome is delivered through the same durable machine an answer takes; it
    carries no answer text."""
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": "silent",
            "answer": None,
            "error": None,
            "delivery_status": DeliveryStatus.PENDING_DELIVERY,
            "updated_at": time.time(),
        }
    )


async def _target_outcome(
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    person: Person | None = None,
    params: dict[str, str] | None = None,
) -> _ToolOutcome:
    """The route's TARGET turn as an outcome: a tool dispatch (which may be silent) or an
    agent run (always answered/error). The single dispatch both the plain and the
    multichannel paths route ordinary text to. ``person`` and ``params`` reach only the tool
    payload; the agent branch ignores both.

    A thread whose effective mode is ``manual`` runs NO target turn: this is where the
    suppression lives, so a pairing turn — dispatched on its own path, never through here —
    stays live and a first-contact greeting still prepends onto the silent outcome."""
    if await effective_mode(route, intake.thread_id) == "manual":
        return await _manual_target_outcome(route, intake, text)
    if route.target_kind == "tool":
        return await _run_tool_turn(route, text, intake.client_address, person, params)
    answer_status, answer, error_detail = await _run_agent_turn(route, text, intake.thread_id, intake.client_address)
    return _ResolvedOutcome(answer_status=answer_status, answer=answer, error=error_detail)


async def _manual_target_outcome(route: ConversationRoute, intake: ConversationRecord, text: str) -> _ToolOutcome:
    """The target turn SUPPRESSED for a manual-mode thread — no agent run, no tool dispatch.
    An agent target that HOLDS thread memory (implements ``append_thread_messages``) has the
    inbound appended to its checkpoint as a ``user`` message, so a later agent turn (once the
    thread returns to ``agent`` mode) reads it as prior context; a memoryless agent target
    (leaves the ABC default), an unregistered agent and a tool target have no thread memory to
    feed, so nothing is appended. Either way the turn produces no reply: the outcome is silent
    (terminal on the channel door, a delivered marker on the api door). An append that FAILS on
    a memory-holding target is a loud client-safe ``error`` outcome, never a silent skip that
    would drop the inbound out of the thread's memory unremarked. A turn that dies after the
    append takes an error outcome without re-running, so the redrive adds no duplicate; an
    api-door caller retrying the same inbound submits a fresh turn (new ``message_id``, no
    inbound dedup there) that appends the line again — accepted over losing the inbound from
    memory."""
    if route.target_kind == "agent":
        agent = _agent_registry().get(route.target_name)
        if agent is not None and supports_thread_append(agent):
            try:
                await agent.append_thread_messages(
                    thread_id=intake.thread_id, messages=[{"role": "user", "content": text}]
                )
            except Exception as exc:
                logger.error(
                    "conversations: manual-mode inbound append for route %r failed", route.route_name, exc_info=exc
                )
                return _tool_error(f"manual-mode append error: {exc}")
    return _SilentOutcome()


def _outcome_record(intake: ConversationRecord, outcome: _ToolOutcome) -> ConversationRecord:
    """Build the completed record from a resolved outcome: an answer goes to
    ``pending_delivery``; a silent outcome is terminal ``silent`` on the channel door and a
    deliverable ``silent`` marker on the api door."""
    if isinstance(outcome, _SilentOutcome):
        return _with_channel_silent(intake) if intake.door == "channel" else _with_api_silent(intake)
    return _with_outcome(intake, outcome.answer_status, outcome.answer, outcome.error)


async def _resolve_turn_record(
    *,
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
) -> ConversationRecord:
    """Run the route's target (or a pairing turn) and build the completed record the
    transition persists. With ``multichannel`` off this is byte-identical to the target turn.
    With it on, the canonical per-accept order runs at the HEAD of this scheduled
    execution — AFTER the door's terminal admission write: ``ensure_provisional`` (the sole
    person WRITE, keying the first-contact greeting and the redeem's own side) → classify
    → dispatch to the pairing turn or the target — and a due first-contact greeting is
    PREPENDED into the answer before it is persisted. ``params`` reach only a tool target's
    payload; a pairing turn ignores them."""
    if multichannel is None:
        return _outcome_record(intake, await _target_outcome(route, intake, text, params=params))

    person, created = await _person_store().ensure_provisional(multichannel.target, multichannel.address_row())
    greeting, greeting_code = await _greeting_and_code(multichannel) if created else (None, None)
    action = classify(text)
    if isinstance(action, Passthrough):
        outcome = await _target_outcome(route, intake, text, person, params)
    else:
        # ``greeting_code`` is the greeting's already-minted code, if any: a first-contact
        # ``/link`` reuses it rather than minting a SECOND code that rotation would delete,
        # leaving the greeting carrying a now-dead code (a Redeem/Unlink ignore it).
        outcome = await _run_pairing_turn(multichannel, person, action, greeting_code)
    return _outcome_record(intake, _with_greeting(outcome, greeting))


def _template_references_code(template: str) -> bool:
    """Whether a validated greeting template references ``{pairing_code}`` (vs a fixed
    string), so a code is minted ONLY when the greeting will actually carry one."""
    return any(field == GREETING_PLACEHOLDER for _literal, field, _spec, _conv in string.Formatter().parse(template))


async def _greeting_and_code(multichannel: _Multichannel) -> tuple[str | None, _MintedCode | None]:
    """The rendered first-contact greeting for a created-now person and the code it minted, or
    ``(None, None)`` when the target configures no template. ``{pairing_code}`` is substituted
    with a freshly minted code (rotating any open one) that is RETURNED so a same-turn
    ``/link`` can present that SAME live code instead of minting a second one; a template with
    no placeholder mints nothing and returns no code."""
    template = multichannel.greeting_template
    if template is None:
        return None, None
    if _template_references_code(template):
        minted = await _pair_code_store().mint(multichannel.minting_conversation())
        return template.format(pairing_code=minted[0]), minted
    return template.format(), None


def _with_greeting(outcome: _ToolOutcome, greeting: str | None) -> _ToolOutcome:
    """Prepend a due greeting into the turn's answer. A silent outcome due a greeting becomes
    an answered greeting-only reply — a greeting, once due, is never silently dropped;
    an error outcome keeps the greeting prefixed to its client-safe text."""
    if greeting is None:
        return outcome
    if isinstance(outcome, _SilentOutcome):
        return _ResolvedOutcome(answer_status="answered", answer=greeting, error=None)
    return _ResolvedOutcome(
        answer_status=outcome.answer_status, answer=f"{greeting}\n\n{outcome.answer}", error=outcome.error
    )


def _pairing_reply(text: str) -> _ResolvedOutcome:
    return _ResolvedOutcome(answer_status="answered", answer=text, error=None)


async def _run_pairing_turn(
    multichannel: _Multichannel, person: Person, action: Link | Unlink | Redeem, greeting_code: _MintedCode | None
) -> _ToolOutcome:
    """Dispatch a classified pairing action against the multichannel target and return its
    resolved outcome. ``Link`` presents ``greeting_code`` when a first-contact greeting
    already minted one this turn (so only ONE code is minted and the greeting's code stays
    live), else mints its own; ``Redeem`` redeems then ensures BOTH sides and merges;
    ``Unlink`` detaches the sending address.

    Error scoping: ONLY the named pairing domain errors (``PairCodeInvalidError``,
    ``CrossTargetMergeError``, ``NotLinkedError``) become the uniform answered refusal — it
    IS the answer, detail logged. ANY other exception (a redis fault, a reset) takes
    the platform's standard client-safe ``error`` outcome, so an infra fault never
    masquerades as an invalid code."""
    try:
        if isinstance(action, Link):
            code, expires_at = greeting_code or await _pair_code_store().mint(multichannel.minting_conversation())
            return _pairing_reply(_link_reply(code, expires_at))
        if isinstance(action, Unlink):
            await _person_store().detach(
                person.person_id,
                door=multichannel.door,
                channel=multichannel.channel,
                our_identity=multichannel.our_identity,
                address=multichannel.address,
            )
            return _pairing_reply(_UNLINKED_TEXT)
        return await _redeem_turn(multichannel, person, action)
    except NotLinkedError as exc:
        logger.info("conversations: /unlink refused on route %r: %s", multichannel.route_name, exc)
        return _pairing_reply(_NOT_LINKED_TEXT)
    except (PairCodeInvalidError, CrossTargetMergeError) as exc:
        logger.info("conversations: pairing refused on route %r: %s", multichannel.route_name, exc)
        return _pairing_reply(_INVALID_CODE_TEXT)
    except Exception as exc:
        logger.error("conversations: pairing turn on route %r failed", multichannel.route_name, exc_info=exc)
        return _tool_error(f"pairing turn error: {exc}")


async def _redeem_turn(multichannel: _Multichannel, person: Person, action: Redeem) -> _ToolOutcome:
    """Redeem a pair code and merge the two persons — behind the brute-force throttle. A
    locked source, and an invalid code, both return the SAME uniform reply (no oracle); a
    valid redeem clears the throttle. The minting side's provisional row is ensured from the
    code's stored value (a tool-minted code may name an address with no admitted inbound
    yet); such a row consumes no greeting — the person is already linked, and the greeting
    predicate fires only at that address's OWN admitted inbound, which this is not."""
    throttle = _redeem_throttle()
    source = multichannel.throttle_source_key()
    if await throttle.is_locked(multichannel.target, source):
        return _pairing_reply(_INVALID_CODE_TEXT)
    try:
        minting = await _pair_code_store().redeem(action.code)
    except PairCodeInvalidError:
        await throttle.record_failure(multichannel.target, source)
        return _pairing_reply(_INVALID_CODE_TEXT)
    await throttle.clear(multichannel.target, source)
    minting_person, _created = await _person_store().ensure_provisional(
        PairingTarget(target_kind=minting.target_kind, target_name=minting.target_name),
        PersonAddress(
            door=minting.door,
            routes=[minting.route_name],
            channel=minting.channel,
            our_identity=minting.our_identity,
            address=minting.address,
            linked_at=datetime.now(UTC),
        ),
    )
    await _person_store().merge(person.person_id, minting_person.person_id)
    return _pairing_reply(_LINKED_TEXT)


def _link_reply(code: str, expires_at: datetime) -> str:
    """The fixed neutral ``/link`` reply carrying the fresh code and its expiry."""
    return (
        f"Your pairing code is {code}. Send it from your other conversation to link the two. "
        f"It expires at {expires_at.isoformat()}."
    )


async def _complete_turn(
    *,
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
) -> ConversationRecord:
    """Run the turn and move its intake record to its outcome (persist before send);
    delivery is the caller's to spawn. A produced answer goes to ``pending_delivery``; a
    silent tool turn goes straight to terminal ``silent`` with nothing to deliver. The
    transition is guarded on the record still being at intake, so a turn finishing after a
    re-drive resolved its record raises rather than overwriting the outcome the client was
    given."""
    completed = await _resolve_turn_record(
        route=route, intake=intake, text=text, multichannel=multichannel, params=params
    )
    if completed.delivery_status is DeliveryStatus.SILENT:
        outcome = await _store().complete_silent(completed)
        verb = "complete_silent"
    else:
        outcome = await _store().complete_turn(completed)
        verb = "complete_turn"
    if outcome != 1:
        raise RuntimeError(
            f"conversations: record {intake.message_id} is no longer at intake "
            f"({verb} answered {outcome}); its outcome was resolved elsewhere and this turn's "
            "outcome is discarded"
        )
    return completed


# -- shed outcomes (the address rate cap) ------------------------------------


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
    completed = _with_outcome(intake, "answered", _SLOW_DOWN_TEXT, None)
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


# -- the channel door: accept ------------------------------------------------


async def accept(
    channel: str,
    our_identity: str,
    client_address: str,
    cap_key: str,
    text: str,
    provider_message_id: str,
    params: dict[str, str] | None = None,
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

    A blank/whitespace-only ``text`` is refused with :class:`BlankInboundTextError` before
    any state is written — there is nothing to run a turn on — for the channel adapter to
    drop like an unrouted message."""
    checked_params = _checked_params(params)
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
    store = _store()

    owner = await store.get_inbound_owner(channel, provider_message_id)
    if owner is not None:
        # Redelivery of a message already accepted: return the prior turn's id.
        return owner

    message_id = str(uuid4())
    admission = get_turn_caps().admit_address(_channel_bucket_key(route.route_name, cap_bucket))
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
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    try:
        await store.create_record(intake, intake_token=intake_token)
        await _refresh_thread_mode_ttl(thread_id)
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
    )
    return message_id


# -- the authed API door -----------------------------------------------------


async def submit_api_message(
    route_name: str,
    external_user_id: str,
    text: str,
    caller_principal: str | None,
    wait_seconds: int,
    params: dict[str, str] | None = None,
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
    this seam runs only a cheap isinstance sweep against its in-process caller."""
    checked_params = _checked_params(params)
    if caller_principal is None or not caller_principal.strip():
        raise UnauthenticatedApiCallerError(
            f"api conversation route {route_name!r} needs an authenticated caller principal and this "
            "deployment resolved none; the api door requires access control to be enabled"
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
    admission = caps.admit_address(_api_bucket_key(route.route_name, caller_principal))
    if admission is not AddressAdmission.ADMIT:
        raise AddressRateLimitedError(
            f"caller {caller_principal!r} is over its rate cap of "
            f"{caps.settings.per_address_turns_per_hour}/hour on route {route.route_name!r}; "
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
        delivery_status=DeliveryStatus.ACCEPTED,
    )
    try:
        await _store().create_record(intake, intake_token=intake_token)
        await _refresh_thread_mode_ttl(thread_id)
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
    )

    if wait_seconds > 0:
        done, _pending = await asyncio.wait({task}, timeout=wait_seconds)
        if task in done and task.exception() is None:
            record = task.result()
            if await mark_wait_delivered(message_id):
                # A turn finished in time — an answer or an explicit silent marker — is
                # returned inline and its callback suppressed exactly as an answer's is.
                return ApiSubmitResult(message_id=message_id, thread_id=thread_id, answer=record.answer_payload())
            # Lost the claim to a racing delivery — fall through to the async shape.

    # Async path: attach the delivery spawn to the task's completion, so exactly one of the
    # wait path and the callback delivers.
    _deliver_when_done(task, message_id)
    return ApiSubmitResult(message_id=message_id, thread_id=thread_id, answer=None)


async def _get_api_route(route_name: str) -> ConversationRoute:
    route = await get_conversations_manager().get_route(route_name)
    if route is None or route.door != "api":
        raise ConversationRouteResolutionError(f"no api conversation route named {route_name!r}")
    return route


# -- the operator send door --------------------------------------------------


class OperatorAppendError(RuntimeError):
    """Appending an operator's message to the thread's agent checkpoint failed, so the send
    is refused and NO record is created — the operator's reply must not stand in the
    transcript while it is absent from the memory a later agent turn reads."""


async def operator_send(
    *,
    route: ConversationRoute,
    thread_id: str,
    client_address: str,
    text: str,
    operator_principal: str,
) -> str:
    """Send an operator's message ``text`` into ``thread_id`` on ``route``, returning its
    record's ``message_id`` (a uuid4). No turn runs: the record is minted already
    ``answered`` carrying the operator's text and handed to the delivery machine, which sends
    it from the route identity exactly as it sends a produced answer (same chunking, ledger
    and receipts). Allowed in either mode; it never flips the mode.

    For an agent target that HOLDS thread memory (implements ``append_thread_messages``) the
    text is appended to the thread's checkpoint as an ``assistant`` message BEFORE the record
    is created, mirroring how an agent's own answer enters its memory, so a later agent turn
    reads the operator's reply as prior context; a memoryless agent target (leaves the ABC
    default), an unregistered agent and a tool target have no thread memory to feed, so nothing
    is appended. The order is resolve → append → create + spawn: an append that fails raises
    :class:`OperatorAppendError` and no record is created, and a create that fails after the
    append also raises — leaving a duplicated memory line a retry would add again, which is
    accepted over a phantom record with no memory behind it.

    The whole append → create → spawn runs under the thread's per-thread FIFO
    (:meth:`TurnCaps.run_reserved`), the same lock in-flight turns take, so the operator's
    write never interleaves a turn's checkpoint and record writes; the HTTP call waits behind
    an in-flight turn. ``run_reserved`` holds the cross-worker thread lease for its span, so an
    operator send and an in-flight turn on the same thread serialize across workers too and
    never fork its checkpoint. As a live-caller sync door the acquisition is bounded by
    ``sync_door_wait_seconds``: a wait past it — behind a turn possibly HITL-paused on another
    worker — raises the loud, retriable :class:`ThreadBusyError` (503) rather than blocking the
    caller past the proxy timeout. A full FIFO raises the loud, retriable
    :class:`ThreadQueueOverflowError` (503) before anything is written."""
    message_id = str(uuid4())
    caps = get_turn_caps()
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        if route.target_kind == "agent":
            agent = _agent_registry().get(route.target_name)
            if agent is not None and supports_thread_append(agent):
                try:
                    await agent.append_thread_messages(
                        thread_id=thread_id, messages=[{"role": "assistant", "content": text}]
                    )
                except Exception as exc:
                    raise OperatorAppendError(
                        f"appending the operator message to thread {thread_id!r} on route {route.route_name!r} "
                        f"failed: {exc}"
                    ) from exc
        record = _new_record(
            route=route,
            message_id=message_id,
            thread_id=thread_id,
            client_address=client_address,
            caller_principal=operator_principal,
            provider_message_id=None,
            inbound_text="",
            delivery_status=DeliveryStatus.PENDING_DELIVERY,
            answer_status="answered",
            answer=text,
            origin="operator",
        )
        await _store().create_record(record)
        await _refresh_thread_mode_ttl(thread_id)
        spawn_delivery(message_id)
    return message_id


# -- turn scheduling under the caps ------------------------------------------


def _schedule_turn(
    caps: TurnCaps,
    *,
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    intake_token: str,
    deliver_on_completion: bool,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
) -> asyncio.Task[ConversationRecord]:
    """Schedule ``intake``'s turn as a background task consuming the caller's reservation;
    returns the task whose result is the completed :class:`ConversationRecord`.

    The intake lease is refreshed OUTSIDE the caps, so a turn queued behind the FIFO reads
    as live too. ``caps`` MUST be the instance the caller reserved on, or the reservation is
    released on a different instance and the slot leaks."""

    async def _run() -> ConversationRecord:
        async with _intake_lease_held(intake.message_id, intake_token), caps.run_reserved(intake.thread_id):
            return await _complete_turn(route=route, intake=intake, text=text, multichannel=multichannel, params=params)

    task = asyncio.create_task(_run())
    _TURN_TASKS.add(task)
    if deliver_on_completion:
        task.add_done_callback(lambda t: _spawn_delivery_on_success(t, intake.message_id))
    else:
        task.add_done_callback(_TURN_TASKS.discard)
    return task


@contextlib.asynccontextmanager
async def _intake_lease_held(message_id: str, token: str) -> AsyncIterator[None]:
    """Refresh ``message_id``'s intake lease for the body's duration, so the intake re-drive
    reads the turn as LIVE and leaves the record to this worker — whether it is running or
    still queued behind the caps."""
    refresher = asyncio.create_task(_refresh_intake_lease(message_id, token))
    try:
        yield
    finally:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher


async def _refresh_intake_lease(message_id: str, token: str) -> None:
    """Re-take the intake lease every ``intake_claim_refresh_seconds`` until the record
    leaves intake or the lease is lost. A refresh that fails is logged and retried — a
    heartbeat that died quietly would let a live turn be reaped as stranded."""
    store = _store()
    settings = store.settings
    while True:
        await asyncio.sleep(settings.intake_claim_refresh_seconds)
        try:
            held = await store.claim_intake(message_id, time.time(), token, settings.intake_claim_lease_seconds)
        except Exception:
            logger.error(
                "conversations: refreshing the intake lease on record %s failed; retrying in %ss",
                message_id,
                settings.intake_claim_refresh_seconds,
                exc_info=True,
            )
            continue
        if held != 1:
            logger.warning(
                "conversations: record %s no longer holds this worker's intake lease (claim returned %d); its "
                "outcome is another worker's to write",
                message_id,
                held,
            )
            return


def _deliver_when_done(task: asyncio.Task[ConversationRecord], message_id: str) -> None:
    """Spawn the record's delivery when its turn task completes successfully."""
    if task.done():
        _spawn_delivery_on_success(task, message_id)
        return
    task.add_done_callback(lambda t: _spawn_delivery_on_success(t, message_id))


def _spawn_delivery_on_success(task: asyncio.Task[ConversationRecord], message_id: str) -> None:
    _TURN_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "conversations: turn task for record %s failed before it wrote an outcome; the record is given an "
            "error outcome and the turn is not re-run",
            message_id,
            exc_info=exc,
        )
        _spawn_intake_resolution(message_id)
        return
    if task.result().delivery_status is DeliveryStatus.SILENT:
        # A channel-door silent turn is terminal already; nothing is ever delivered. An
        # api-door silent turn sits at pending_delivery and is delivered like an answer.
        return
    spawn_delivery(message_id)


# -- resolving a record left mid-turn ----------------------------------------


def _spawn_intake_resolution(message_id: str) -> None:
    """Resolve a record this worker left at intake, in this worker, now — this worker owns
    it, and waiting out its intake lease would hold the message unanswered for that long."""
    task = asyncio.create_task(_resolve_stranded_intake(message_id))
    _TURN_TASKS.add(task)
    task.add_done_callback(_on_intake_resolution_done)


def _on_intake_resolution_done(task: asyncio.Task[None]) -> None:
    _TURN_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("conversations: resolving a record left at intake by a failed turn task failed", exc_info=exc)


async def _resolve_stranded_intake(message_id: str) -> None:
    """Resolve a record this worker left at intake — an interrupted commit or a turn task
    that died. A record that has already left intake keeps the outcome it carries; one still
    at intake is arbitrated against its inbound pair."""
    store = _store()
    record = await store.get_record(message_id)
    if record is None:
        logger.warning(
            "conversations: record %s is gone, so the turn that failed on it leaves nothing to resolve", message_id
        )
        return
    if record.delivery_status is not DeliveryStatus.ACCEPTED:
        logger.info(
            "conversations: record %s already carries a %s outcome; the turn task's failure resolves nothing",
            message_id,
            record.delivery_status.value,
        )
        return
    await _arbitrate_stranded_intake(store, record)


async def _arbitrate_stranded_intake(store: ConversationRecordStore, record: ConversationRecord) -> None:
    """Resolve one record left at intake against its inbound pair: the record the pair is
    committed to takes the error outcome and delivers it; one that lost the pair to another
    attempt owns nothing and is discarded."""
    if await _owns_inbound_claim(store, record):
        await _fail_stranded_turn(store, record)
        return
    await store.delete_record(record)
    logger.warning(
        "conversations: intake record %s lost the inbound claim for %r on channel %r to another attempt and was "
        "discarded",
        record.message_id,
        record.provider_message_id,
        record.channel,
    )


async def redrive_accepted() -> None:
    """Resolve every record left in ``accepted`` by a worker that DIED mid-turn.

    The intake lease is the liveness test and it is taken FIRST: a record whose lease is
    still live belongs to a turn running on a sibling worker and is left untouched, so the
    sweep never reaps an in-flight turn. Only a record whose lease has LAPSED is adopted,
    then arbitrated against the inbound claim (a get-or-set): one the claim names someone
    else for is discarded. An adopted record takes the ``error`` outcome and its turn is
    never re-run — a turn dispatches authorized tools, so it is not idempotent."""
    store = _store()
    token = uuid4().hex
    for record in await store.list_by_status(frozenset({DeliveryStatus.ACCEPTED})):
        try:
            adopted = await store.claim_intake(
                record.message_id, time.time(), token, store.settings.intake_claim_lease_seconds
            )
            if adopted != 1:
                logger.info(
                    "conversations: intake record %s was not adopted by the re-drive (claim returned %d); its turn "
                    "is live on another worker, or its outcome has already landed",
                    record.message_id,
                    adopted,
                )
                continue
            await _arbitrate_stranded_intake(store, record)
        except Exception:
            # One failing record must not abandon every other stranded record in the pass;
            # the next sweep re-drives this one.
            logger.error(
                "conversations: re-driving stranded intake record %s failed; skipped this pass",
                record.message_id,
                exc_info=True,
            )
            continue


async def _owns_inbound_claim(store: ConversationRecordStore, record: ConversationRecord) -> bool:
    """Whether ``record`` is the one its inbound pair is committed to. An api-door record
    has no provider id to dedupe on, so it is its own authority."""
    if record.channel is None or record.provider_message_id is None:
        return True
    owner = await store.claim_inbound(record.channel, record.provider_message_id, record.message_id)
    return owner == record.message_id


async def _fail_stranded_turn(store: ConversationRecordStore, record: ConversationRecord) -> None:
    """Give an intake record the error outcome its interrupted turn never produced and
    spawn its delivery — the one resolution both the in-process watcher and the periodic
    re-drive apply. Losing the guarded transition leaves the existing outcome standing."""
    completed = _with_outcome(record, "error", _ERROR_ANSWER_TEXT, "turn was interrupted before it produced an answer")
    outcome = await store.complete_turn(completed)
    if outcome != 1:
        logger.warning(
            "conversations: intake record %s left intake while it was being re-driven (complete_turn answered %d); "
            "its outcome stands as written",
            record.message_id,
            outcome,
        )
        return
    logger.error(
        "conversations: record %s was stranded mid-turn; the turn is NOT re-run and a client-safe error outcome "
        "is delivered instead",
        record.message_id,
    )
    spawn_delivery(record.message_id)


__all__ = [
    "ApiSubmitResult",
    "ConversationRouteResolutionError",
    "OperatorAppendError",
    "UnauthenticatedApiCallerError",
    "accept",
    "operator_send",
    "redrive_accepted",
    "submit_api_message",
]
