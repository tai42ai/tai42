"""The turn engine — turns an accepted message into an agent turn.

One inbound message (channel door :func:`accept`, authed API door
:func:`submit_api_message`) resolves to its route, runs that route's agent IN-PROCESS under
the route's execution key, and persists the produced answer as a durable record the delivery
executor sends back.

A route targets a TOOL or an AGENT, and the two kinds deliver a PARKED target's resumed answer
by different paths — the canonical statement of this asymmetry, cited (not restated) at both
park branches below. A parked tool target resumes out of band and its resuming consumer owns
delivery-back: the consumer sends its late reply through its own send steps, so the platform
binds no completion tool and ends the turn silently. A parked agent target has no self-delivery,
so the platform binds the completion tool for the run and posts the resumed answer back into
this thread.
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
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

from tai42_contract.agent import Agent
from tai42_contract.agent.events import InterruptFinal, MessageFinal, StructuredFinal, SuspendedFinal
from tai42_contract.conversations import (
    GREETING_PLACEHOLDER,
    AnswerPart,
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
    joined_answer_text,
)
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    MediaItem,
    SuspendedInteraction,
    reset_park_completion,
    set_park_completion,
)
from tai42_contract.monitoring import RunAttribution
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
from tai42_skeleton.tools.attribution import run_attribution
from tai42_skeleton.tools.turn_budget import drive_live_caller_astream

logger = logging.getLogger(__name__)

# The registered name of the hidden completion-delivery tool. The conversation door binds it
# (``set_park_completion``) around an agent turn, so an async ``ask_user`` may park with a
# path back to this thread; a resumed run's driver fires it with ``{thread_id, result}`` and it
# mints the answered record + spawns delivery. Must equal the registered tool name.
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


#: Internal sentinel: the agent turn parked on an async ``ask_user`` instead of answering.
#: Its resumed answer is delivered out of band by the completion continuation
#: (:data:`COMPLETION_TOOL_NAME`), so the turn produces no reply now.
class _AgentParked:
    __slots__ = ()


_AGENT_PARKED = _AgentParked()


async def _drain_answer(agent: Agent, text: str, thread_id: str) -> str | _AgentParked:
    """Run the agent to its terminal event and return the answer text, or the
    :data:`_AGENT_PARKED` sentinel when the agent parked on an async ``ask_user``. A
    structured final is serialized; an interrupt is not answerable by a background turn and
    is raised."""
    structured: StructuredFinal | None = None
    message: MessageFinal | None = None
    # Route the live-caller drive through the shared seam so the turn is budgeted and its
    # trace attributed — this bridge holds a live client, so it is not detached-exempt.
    async for event in drive_live_caller_astream(agent.astream(user_message=text, thread_id=thread_id)):
        if isinstance(event, SuspendedFinal):
            # The agent parked on an async ask_user. The conversation door bound a completion
            # tool around the run, so its resumed answer is delivered out of band into this
            # thread — the turn produces no reply now.
            return _AGENT_PARKED
        if isinstance(event, InterruptFinal):
            raise RuntimeError(f"agent raised an interrupt ({event.interrupt_id}) a background turn cannot answer")
        if isinstance(event, StructuredFinal):
            structured = event
        elif isinstance(event, MessageFinal):
            message = event
    if structured is not None:
        # F1 (strict ruling): an agent's structured final is ALWAYS serialized to one
        # string, even when its data is a JSON array of strings — an agent's structured
        # output may legitimately be a string array as DATA, so there is no magic-array
        # detection here. Ordered multi-message answers come from TOOL routes only (see
        # ``_tool_reply``, which may emit an array of parts); an agent stays single-string
        # until it has an explicit multi-message output contract.
        return _serialize_structured(structured.data)
    if message is not None:
        return message.text
    return ""


async def _run_agent_turn(route: ConversationRoute, text: str, thread_id: str, client_address: str) -> _ToolOutcome:
    """Run one agent turn as the route's execution key and return its resolved outcome. The
    identity is bound for the turn's duration and the run authorized against it before the
    agent runs. A denied run, a mid-turn error or an empty answer becomes a client-safe
    ``error`` outcome; a run that PARKS on an async ``ask_user`` becomes a silent outcome —
    its resumed answer delivers out of band through the completion continuation.

    The turn-scoped bridge context is established around the agent invocation, so an
    in-process builtin the agent calls (``set_conversation_mode``) reads the CURRENT
    conversation's thread from it — the same contextvar propagation the bound execution
    identity relies on. The completion continuation (:data:`COMPLETION_TOOL_NAME`) is bound
    for the run's duration too: it is the deferred-response delivery path that lets an async
    ask_user PARK here (a run with none bound refuses the ask loudly pre-persist), and a
    resumed run's final answer fires it to post the reply back into this thread."""
    agent = _agent_registry().get(route.target_name)
    if agent is None:
        return _tool_error(f"agent {route.target_name!r} is not registered", route)
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
                # An agent target has no self-delivery — per the module docstring's park-delivery
                # paths, the platform binds the completion tool so a resumed run posts back here.
                completion_token = set_park_completion(COMPLETION_TOOL_NAME)
                try:
                    answer = await _drain_answer(agent, text, thread_id)
                finally:
                    reset_park_completion(completion_token)
    except PermissionDenied as exc:
        return _tool_error(f"turn denied: {exc}", route)
    except Exception as exc:
        # A failed turn becomes a logged error OUTCOME, not a swallowed error.
        logger.error("conversations: turn for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"turn error: {exc}", route)
    if isinstance(answer, _AgentParked):
        return _SilentOutcome()
    if not answer.strip():
        return _tool_error("agent produced an empty answer", route)
    return _ResolvedOutcome(answer_status="answered", parts=[_text_part(answer)], error=None)


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
    """A turn that produced an outcome to deliver: an ``answered`` reply or a client-safe
    ``error``. ``parts`` is the ORDERED, non-empty list of rich :class:`AnswerPart` messages
    the turn produced — one for a single-message answer, several for an ordered multi-message
    one (a tool route emitting an array of strings and/or part objects). ``answer`` is the
    part MESSAGE texts joined with a blank line — the whole-text form every legacy reader
    keeps consuming."""

    answer_status: Literal["answered", "error"]
    parts: list[AnswerPart]
    error: str | None

    @property
    def answer(self) -> str:
        """The part messages as one joined string — what intake dedup, transcripts and the
        api door body read, and byte-identical to the old single ``answer`` for one part. A
        media-only part contributes nothing, so an all-media outcome joins to ``""``."""
        return joined_answer_text(self.parts)


def _text_part(text: str) -> AnswerPart:
    """A plain text-only :class:`AnswerPart` — the shape the platform's own replies (agent
    answers, tool string replies, greetings, error/slow-down text, pairing replies) take."""
    return AnswerPart(message=text)


#: A tool turn resolves to exactly one of these two shapes — no third, coercible state.
_ToolOutcome = _SilentOutcome | _ResolvedOutcome

#: A freshly minted pair code and its expiry, as :meth:`ConversationPairCodeStore.mint` returns.
_MintedCode = tuple[str, datetime]


def _error_answer_text(route: ConversationRoute | None) -> str:
    """The guest-facing text for a failed turn: the route's configured ``error_reply_text``
    when it carries one, else the built-in English default. A ``None`` route (no route in
    scope) falls back to the default. Only the guest-facing ``answer`` resolves through the
    route — the record's ``error`` detail and the logs keep the built-in wording."""
    return (route.error_reply_text if route is not None else None) or _ERROR_ANSWER_TEXT


def _tool_error(detail: str, route: ConversationRoute | None = None) -> _ResolvedOutcome:
    return _ResolvedOutcome(answer_status="error", parts=[_text_part(_error_answer_text(route))], error=detail)


async def _run_tool_turn(
    route: ConversationRoute,
    text: str,
    thread_id: str,
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
    whose detail is logged, never delivered.

    The generic completion continuation (:data:`DELIVER_TOOL_COMPLETION_NAME`) is bound for
    the dispatch's duration, carrying this turn's ``thread_id`` as the opaque delivery
    address. It lets ANY parking tool async-park with a path back to this thread: when the
    parked tool's own resumer drives to a clean terminal out of band, it fires the completion
    with that address so the deferred outcome is mapped through the route's ``reply_expr`` and
    posted back here. A tool that never parks never fires it; the turn stays byte-identical to
    a plain dispatch. The platform learns nothing of the tool's resume machinery — only that a
    park may deliver later through this bound address."""
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
        return _tool_error(f"payload_expr error ({type(exc).__name__})", route)
    try:
        async with bind_execution_identity(route.execution_key, bound_fingerprint=route.execution_key_fingerprint):
            # Bind the generic tool-route completion for the dispatch: a parking tool captures
            # it, and its resumer fires the deferred outcome back to THIS thread out of band.
            # A non-parking tool never reads it. Reset in a finally so it never leaks past the
            # dispatch.
            # Pin the ORIGINATING route beside the delivery thread: a linked person may write
            # from a different route before the resume, and the completion must map the outcome
            # through THIS route's reply_expr, not the route the thread's newest record names.
            completion_token = set_park_completion(
                DELIVER_TOOL_COMPLETION_NAME,
                {"delivery_thread_id": thread_id, "route_name": route.route_name},
            )
            try:
                # ``offload_sync``: a synchronous tool runs off the event loop, matching the
                # meta-executor door, so a blocking tool cannot starve the turn engine.
                result = await _tools().run_tool(route.target_name, kwargs, offload_sync=True)
            finally:
                reset_park_completion(completion_token)
    except PermissionDenied as exc:
        return _tool_error(f"turn denied: {exc}", route)
    except Exception as exc:
        logger.error("conversations: tool turn for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"turn error: {exc}", route)
    if isinstance(result, SuspendedInteraction):
        # The tool parked the caller on an async ask (a generic contract sentinel —
        # the turn learns nothing of the driver's resume state): produce no reply and
        # end the turn silently. No completion tool is bound — per the module docstring's
        # park-delivery paths, a tool target's resuming consumer owns delivery-back.
        return _SilentOutcome()
    try:
        reply = await _tool_reply(route, result)
    except Exception as exc:
        logger.error("conversations: mapping the tool result for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"reply_expr error: {exc}", route)
    parts = _reply_parts(reply)
    if parts is None:
        return _SilentOutcome()
    return _ResolvedOutcome(answer_status="answered", parts=parts, error=None)


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


async def _tool_reply(route: ConversationRoute, result: object) -> str | list[AnswerPart] | None:
    """The reply a tool result maps to: ``None`` for a silent outcome, a single string, or an
    ORDERED LIST OF RICH :class:`AnswerPart` messages the delivery machine sends as separate
    messages in order. No ``reply_expr`` → the result must itself be ``None``, a string, or a
    list. Otherwise the jq program over the raw result, which MUST emit exactly one value and
    it MUST be null, a string, or an array.

    A reply ARRAY is the multi-message authoring surface: each element is EITHER a plain
    string (shorthand for a text-only part) or a part OBJECT (``{message, media?, options?,
    template?}``) — both normalize to the one internal :class:`AnswerPart` model. An empty
    array, a blank string element, or a malformed part object (an unknown key, a bad shape) is
    a loud ``ValueError`` — never silently coerced (order is meaning; parts are strict from
    birth)."""
    if route.reply_expr is None:
        if result is None or isinstance(result, str):
            return result
        if isinstance(result, list):
            return _checked_reply_parts(result)
        raise ValueError(
            "a tool target with no reply_expr must return null, a string, or a list of parts, "
            f"returned {type(result).__name__}"
        )
    # Bounded at one, so an over-emitting program is capped rather than materialized whole.
    values = await run_jq_bounded(route.reply_expr, result, 1)
    if len(values) != 1:
        raise ValueError(f"reply_expr must emit exactly one value, emitted {'more than one' if values else 'none'}")
    reply = values[0]
    if reply is None or isinstance(reply, str):
        return reply
    if isinstance(reply, list):
        return _checked_reply_parts(reply)
    raise ValueError(f"reply_expr must emit null, a string, or an array of parts, emitted {type(reply).__name__}")


def _checked_reply_parts(value: list[object]) -> list[AnswerPart]:
    """A tool reply array normalized to ordered :class:`AnswerPart` messages: non-empty, and
    every element EITHER a plain string (a text-only part) or a part object. A blank string, a
    malformed/unknown-key part object (``AnswerPart`` is ``extra="forbid"``), or an
    unsupported element type is a loud ``ValueError`` — an empty array has no message to send,
    and a bad element would deliver an empty or garbled part."""
    if not value:
        raise ValueError("a tool reply array must carry at least one message, emitted an empty array")
    parts: list[AnswerPart] = []
    for index, element in enumerate(value):
        if isinstance(element, str):
            if not element.strip():
                raise ValueError(f"tool reply array element {index} is a blank string; a text part must be non-blank")
            parts.append(_text_part(element))
        elif isinstance(element, dict):
            try:
                parts.append(AnswerPart.model_validate(element))
            except ValueError as exc:
                raise ValueError(f"tool reply array element {index} is not a valid part: {exc}") from exc
        else:
            raise ValueError(
                f"tool reply array element {index} must be a string or a part object, got {type(element).__name__}"
            )
    return parts


def _reply_parts(reply: str | list[AnswerPart] | None) -> list[AnswerPart] | None:
    """The ordered parts a :func:`_tool_reply` result delivers, or ``None`` for a silent
    outcome. A ``None`` reply and a blank single string are both silent; a non-blank single
    string is one text part; a list is already normalized and validated by
    :func:`_tool_reply`, so it passes through as the ordered parts."""
    if reply is None:
        return None
    if isinstance(reply, str):
        return [_text_part(reply)] if reply.strip() else None
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
    answer_parts: list[AnswerPart] | None = None,
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
        answer_parts=answer_parts,
        error=error,
        created_at=now,
        updated_at=now,
    )


def _answer_fields(parts: list[AnswerPart]) -> tuple[str, list[AnswerPart] | None]:
    """The ``(answer, answer_parts)`` a record stores for an ordered ``parts`` list: the part
    MESSAGES joined into the whole text every legacy reader consumes, and the parts list
    ITSELF only when it adds something over that text — more than one part, or one part
    carrying media/options/a template. A single PLAIN-TEXT answer carries ``answer_parts=None``
    (byte-parity with the pre-parts single answer), mirroring ``ConversationAnswer.parts``. A
    media-only part contributes nothing to ``answer``, so an all-media answer stores ``answer=""``
    with the parts."""
    answer = joined_answer_text(parts)
    if len(parts) == 1 and parts[0].is_plain_text():
        return answer, None
    return answer, parts


def _with_outcome(
    intake: ConversationRecord, answer_status: AnswerStatus, parts: list[AnswerPart], error_detail: str | None
) -> ConversationRecord:
    """``intake`` carrying a produced outcome and moved to ``pending_delivery`` — the shape
    :meth:`ConversationRecordStore.complete_turn` requires. ``parts`` is the ordered
    message list; the record stores the joined ``answer`` and, for a multi-message answer,
    the ``answer_parts`` the delivery machine sends one message at a time."""
    answer, answer_parts = _answer_fields(parts)
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": answer_status,
            "answer": answer,
            "answer_parts": answer_parts,
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
    stays live and a first-contact greeting still prepends onto the silent outcome.

    The run's generic attribution is deposited HERE — the single seam both the tool and
    agent target turns route through — so whichever run chokepoint the target reaches (the
    tool ``run_tool`` seam or the agent ``drive_live_caller_astream`` seam) stamps its
    trace with this conversation's identity. tai42 stays flow-agnostic: it deposits only
    generic dimensions (a person-or-address user, the resolved thread as session, the
    route as a tag, the channel/our_identity as metadata) and interprets none of them."""
    if await effective_mode(route, intake.thread_id) == "manual":
        return await _manual_target_outcome(route, intake, text)
    with run_attribution(_conversation_attribution(route, intake, person)):
        if route.target_kind == "tool":
            return await _run_tool_turn(route, text, intake.thread_id, intake.client_address, person, params)
        return await _run_agent_turn(route, text, intake.thread_id, intake.client_address)


def _conversation_attribution(
    route: ConversationRoute, intake: ConversationRecord, person: Person | None
) -> RunAttribution:
    """The generic :class:`RunAttribution` a conversation turn's run trace is stamped with.

    ``user_id`` is person-FIRST — a resolved (linked or provisional) person's stable
    ``person_id`` — falling back to the raw ``{channel}:{client_address}`` the door saw
    when no person exists (the plain, non-multichannel path). ``session_id`` is the
    RESOLVED thread — the route-keyed ``bridge:{route}:{address}`` or the ``@person``
    aggregated thread — so a person's runs across channels group under one session.
    ``tags`` carry the route; ``metadata`` carries the channel + our-identity (present
    only for a channel door). All generic — the platform assigns no meaning."""
    metadata: dict[str, Any] = {}
    if intake.channel is not None:
        metadata["channel"] = intake.channel
    if intake.our_identity is not None:
        metadata["our_identity"] = intake.our_identity
    if person is not None:
        user_id = person.person_id
    elif intake.channel is not None:
        user_id = f"{intake.channel}:{intake.client_address}"
    else:
        # API door with no resolved person: the address alone — never a literal
        # "None:" prefix, since this string doubles as the erasure key.
        user_id = intake.client_address
    return RunAttribution(
        user_id=user_id,
        session_id=intake.thread_id,
        tags=[f"route:{route.route_name}"],
        metadata=metadata,
    )


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
                return _tool_error(f"manual-mode append error: {exc}", route)
    return _SilentOutcome()


def _outcome_record(intake: ConversationRecord, outcome: _ToolOutcome) -> ConversationRecord:
    """Build the completed record from a resolved outcome: an answer goes to
    ``pending_delivery``; a silent outcome is terminal ``silent`` on the channel door and a
    deliverable ``silent`` marker on the api door."""
    if isinstance(outcome, _SilentOutcome):
        return _with_channel_silent(intake) if intake.door == "channel" else _with_api_silent(intake)
    return _with_outcome(intake, outcome.answer_status, outcome.parts, outcome.error)


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
        outcome = await _run_pairing_turn(multichannel, person, action, greeting_code, route)
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
    """Prepend a due greeting as its own LEADING message. A greeting is a message of its
    own, so it becomes the first ordered part ahead of the turn's parts; a silent outcome
    due a greeting becomes an answered greeting-only reply — a greeting, once due, is never
    silently dropped; an error outcome keeps the greeting ahead of its client-safe text. The
    joined answer is byte-identical to the old ``f"{greeting}\n\n{answer}"`` prefix for a
    single-part outcome."""
    if greeting is None:
        return outcome
    if isinstance(outcome, _SilentOutcome):
        return _ResolvedOutcome(answer_status="answered", parts=[_text_part(greeting)], error=None)
    return _ResolvedOutcome(
        answer_status=outcome.answer_status, parts=[_text_part(greeting), *outcome.parts], error=outcome.error
    )


def _pairing_reply(text: str) -> _ResolvedOutcome:
    return _ResolvedOutcome(answer_status="answered", parts=[_text_part(text)], error=None)


async def _run_pairing_turn(
    multichannel: _Multichannel,
    person: Person,
    action: Link | Unlink | Redeem,
    greeting_code: _MintedCode | None,
    route: ConversationRoute,
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
        return _tool_error(f"pairing turn error: {exc}", route)


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
    media: list[MediaItem] | None = None,
    options: list[str] | None = None,
) -> str:
    """Send an operator's message ``text`` into ``thread_id`` on ``route``, returning its
    record's ``message_id`` (a uuid4). No turn runs: the record is minted already
    ``answered`` carrying the operator's text and handed to the delivery machine, which sends
    it from the route identity exactly as it sends a produced answer (same chunking, ledger
    and receipts). Allowed in either mode; it never flips the mode.

    ``media`` and ``options`` are OPTIONAL richer-send forms: when either is set the reply is
    stored as a single rich :class:`AnswerPart` (``message=text`` carrying the media/options),
    so the delivery machine sends the operator's message with its media/options exactly as it
    sends a produced rich part; with neither set the record stays a plain single-message
    answer (byte-parity with the pre-rich operator send). A contract-invalid media/options
    value (an empty list, an over-cap value) raises before any state is written.

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
    # A rich operator send (media/options present) stores one :class:`AnswerPart` carrying
    # the text plus its media/options — the shape the delivery machine sends as a rich part.
    # A plain send keeps ``answer=text`` with no parts, byte-identical to the pre-rich path
    # (and unbounded by the part message cap, which only governs a rich part's text).
    if media is None and options is None:
        answer: str = text
        answer_parts: list[AnswerPart] | None = None
    else:
        answer, answer_parts = _answer_fields([AnswerPart(message=text, media=media, options=options)])
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
            answer=answer,
            answer_parts=answer_parts,
            origin="operator",
        )
        await _store().create_record(record)
        await _refresh_thread_mode_ttl(thread_id)
        spawn_delivery(message_id)
    return message_id


# -- resumed-answer delivery (the completion continuation) -------------------


class CompletionDeliveryError(RuntimeError):
    """The resumed answer of an async-parked agent turn could not be delivered — its thread
    could not be reversed to a live route + address. Raised loudly so the completion
    continuation's at-least-once seam retains and retries rather than dropping the answer."""


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
        person = await _person_store().get_by_id(person_id)
        if person is None:
            raise CompletionDeliveryError(f"no person for parked thread {thread_id!r}")
        person_routes = sorted({route for address in person.addresses for route in address.routes})
        newest = await _store().list_person_thread_records(
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
    route = await get_conversations_manager().get_route(route_name)
    if route is None:
        raise CompletionDeliveryError(f"route {route_name!r} for parked thread {thread_id!r} no longer exists")
    return route, client_address


async def deliver_agent_completion(thread_id: str, result: Any, completion_id: str) -> dict[str, str | None]:
    """Deliver a resumed agent turn's FINAL answer back into its originating thread.

    The completion continuation the conversation door bound around a parked agent turn: a
    resumed run's driver fires it with ``{thread_id, result, completion_id}`` when the run
    drives to a clean terminal out of band. It reverses the thread to its route + address,
    mints the answer as an already-``answered`` record keyed by ``completion_id``, and hands it
    to the SAME delivery machine a produced answer takes — never appending to the agent's memory
    (the resumed run already recorded the answer in its own checkpoint).

    ``completion_id`` is the stable idempotency id of the resolved super-step: the delivery
    record is keyed by it, so a lease-lapse re-drive that fires the completion a second time
    with the same id finds the record already committed and is a benign no-op — the durable
    record commit is the exactly-once point. A blank resumed answer delivers the SAME
    client-safe error reply the fresh-turn path delivers for an empty answer, so it is not a
    silent non-delivery. Returns ``{"message_id": completion_id}`` for the delivered (or already
    delivered) record. An unresolvable thread raises :class:`CompletionDeliveryError` loudly."""
    existing = await _store().get_record(completion_id)
    if existing is not None:
        # A redelivered completion for a super-step whose durable record already committed (a
        # lease-lapse re-drive): the exactly-once point is passed, so this is a benign no-op.
        return {"message_id": completion_id}
    text = _serialize_structured(result)
    route, client_address = await _resolve_completion_target(thread_id)
    # A blank resumed answer delivers the SAME client-safe error text the fresh-turn path
    # replies for an empty answer, so the client sees a consistent outcome either way — the
    # route's own ``error_reply_text`` when it carries one. It rides the operator record as an
    # ``answered`` reply (an operator record is always answered) — the text is the reply, there
    # is no client turn to mark ``error`` against.
    answer = text if text.strip() else _error_answer_text(route)
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
    await _store().create_record(record)
    await _refresh_thread_mode_ttl(thread_id)
    spawn_delivery(completion_id)
    return {"message_id": completion_id}


async def deliver_tool_completion(
    delivery_thread_id: str,
    completion_id: str,
    result: Any = None,
    status: str = PARK_COMPLETION_FAILED,
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
    :data:`PARK_COMPLETION_SUCCEEDED` maps ``result`` via ``reply_expr``; ANY other value —
    including the fail-safe default :data:`PARK_COMPLETION_FAILED` an unstamped fire falls back
    to — is a non-success terminal (the route carries no error mapping) delivered as the
    uniform client-safe notice, so a failed/stopped/aborted resume is never silently dropped
    and an unstamped fire never pushes a non-success payload through ``reply_expr``. A
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
    benign no-op. Returns ``{"message_id": completion_id}`` for the delivered (or already
    delivered) record, or ``{"message_id": None}`` for a silent outcome. Generic: it knows
    nothing of the parking tool, only the route contract and the opaque context it reverses. An
    unresolvable thread or a vanished originating route raises :class:`CompletionDeliveryError`
    loudly so the resumer's at-least-once seam retains and retries rather than dropping the
    outcome."""
    existing = await _store().get_record(completion_id)
    if existing is not None:
        # A redelivered completion for a terminal whose durable record already committed: the
        # exactly-once point is passed, so this is a benign no-op.
        return {"message_id": completion_id}
    route, client_address = await _resolve_completion_target(delivery_thread_id)
    # Map through the ORIGINATING route's reply_expr (pinned at park time), not the reversed
    # delivery route — a linked person may have written from a different route since the park,
    # whose reply_expr would map the terminal wrongly.
    mapping_route = route
    if route_name is not None:
        pinned = await get_conversations_manager().get_route(route_name)
        if pinned is None:
            raise CompletionDeliveryError(
                f"originating route {route_name!r} for parked thread {delivery_thread_id!r} no longer exists"
            )
        mapping_route = pinned
    if status == PARK_COMPLETION_SUCCEEDED:
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
            # The guest-facing notice resolves through the DELIVERY route (where the record is
            # filed and the guest is conversing), so its own ``error_reply_text`` applies.
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
    await _store().create_record(record)
    await _refresh_thread_mode_ttl(delivery_thread_id)
    spawn_delivery(completion_id)
    return {"message_id": completion_id}


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
    re-drive apply. Losing the guarded transition leaves the existing outcome standing.

    The intake record carries its ``route_name``, so the guest-facing text resolves the
    route's ``error_reply_text`` best-effort: the route is looked up through the conversations
    manager and ANY failure (manager unavailable, route gone, exception) falls back to the
    built-in default. This is an interrupted-turn/lease-lapse repair path, so it must never be
    less robust than a bare default — the lookup only ever upgrades the text, never blocks the
    outcome. Only the guest-facing ``answer`` resolves through the route; the record's ``error``
    detail and the logs keep the built-in wording."""
    try:
        route = await get_conversations_manager().get_route(record.route_name)
    except Exception:
        route = None
    completed = _with_outcome(
        record, "error", [_text_part(_error_answer_text(route))], "turn was interrupted before it produced an answer"
    )
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
    "COMPLETION_TOOL_NAME",
    "DELIVER_TOOL_COMPLETION_NAME",
    "ApiSubmitResult",
    "CompletionDeliveryError",
    "ConversationRouteResolutionError",
    "OperatorAppendError",
    "UnauthenticatedApiCallerError",
    "accept",
    "deliver_agent_completion",
    "deliver_tool_completion",
    "operator_send",
    "redrive_accepted",
    "submit_api_message",
]
