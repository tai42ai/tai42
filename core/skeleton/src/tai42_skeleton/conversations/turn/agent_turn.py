"""Run one agent turn to its resolved outcome, as the route's execution key."""

from __future__ import annotations

import logging

from tai42_contract.agent import Agent
from tai42_contract.agent.events import InterruptFinal, MessageFinal, StructuredFinal, SuspendedFinal
from tai42_contract.conversations import ConversationRoute
from tai42_contract.interactions import reset_park_completion, set_park_completion
from tai42_contract.template import TemplatedText

from tai42_skeleton.authz.execution import authorize_execution_agent_run, bind_execution_identity
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.outcome import (
    _AGENT_PARKED,
    _AgentParked,
    _ResolvedOutcome,
    _serialize_structured,
    _SilentOutcome,
    _text_part,
    _tool_error,
    _ToolOutcome,
)
from tai42_skeleton.conversations.turn_context import BridgeTurnContext, bridge_turn_context
from tai42_skeleton.operations.errors import PermissionDenied
from tai42_skeleton.tools.turn_budget import drive_live_caller_astream

logger = logging.getLogger("tai42_skeleton.conversations.turn")


async def _drain_answer(agent: Agent, text: str, thread_id: str) -> str | _AgentParked:
    """Run the agent to its terminal event and return the answer text, or the
    :data:`_AGENT_PARKED` sentinel when the agent parked on an async ``ask_user``. A
    structured final is serialized; an interrupt is not answerable by a background turn and
    is raised."""
    structured: StructuredFinal | None = None
    message: MessageFinal | None = None
    # Route the live-caller drive through the shared seam so the turn is budgeted and its
    # trace attributed — this bridge holds a live client, so it is not detached-exempt.
    async for event in drive_live_caller_astream(
        agent.astream(user_message=TemplatedText(content=text), thread_id=thread_id)
    ):
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
        # An agent's structured final is ALWAYS serialized to one
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
    for the run's duration too, carrying this turn's ``thread_id`` as the opaque delivery
    address: it is the deferred-response delivery path that lets an async ask_user PARK here (a
    run with none bound refuses the ask loudly pre-persist), and a resumed run's final answer
    fires it with that address to post the reply back into this thread."""
    # ``completion_delivery`` imports this module, so the constant is read lazily to keep
    # the door↔turn package free of an import cycle.
    from tai42_skeleton.conversations.turn.completion_delivery import COMPLETION_TOOL_NAME

    agent = accessors._agent_registry().get(route.target_name)
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
                # This turn's thread rides the OPAQUE completion context, keyed by the delivery
                # tool's own routing parameter: a resuming driver fires the generic contract
                # payload (that context merged with ``{result, completion_id, status}``) without
                # ever naming the thread itself, exactly as the tool-route sibling below does.
                completion_token = set_park_completion(COMPLETION_TOOL_NAME, {"thread_id": thread_id})
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
