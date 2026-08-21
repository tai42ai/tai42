"""The ``before_model`` park hook for a park-capable agent loop.

A side-effect-free middleware placed at the head of the agent loop (both the tools-agent
and deep-agent loops mount it). Its whole job is to turn the reserved async-park marker
(stamped onto a ToolMessage by the in-process client-tool seam when an async ``ask_user``
returned its sentinel) into a single graph ``interrupt`` for the super-step, and — on
resume — to substitute each parked ToolMessage's answer back in place.

Side-effect-free is load-bearing: ``interrupt`` re-executes the whole node on resume, so
the scan-and-substitute must be safe to run twice. It is — the scan is pure, and the
substitution is an idempotent replace-by-id through the messages reducer.

The hook must run BEFORE any message-compacting hook, or a compaction could evict a
marked ToolMessage before the park is recognized. A message compactor runs through
``wrap_model_call`` (during the model node, skipped on a park super-step), and this is the
only ``before_model`` hook in the stack, so it is the loop's first per-step hook by
construction; a wiring test pins that.
"""

from __future__ import annotations

from typing import Any, Final, cast

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.prebuilt.tool_node import msg_content_output
from langgraph.types import interrupt
from tai42_contract.interactions import read_suspended_interaction_marker

from tai42_agents._internal.park.errors import AgentParkMarkerError

# The reserved key a park interrupt's VALUE carries, so the drive wrapper recognizes a
# park interrupt by its value shape (never a name). Internal to the agents plugin — the
# platform contract knows only the generic ToolMessage marker, not this interrupt payload.
AGENT_PARK_PAYLOAD_KEY: Final[str] = "tai42:agent_park"


def _trailing_tool_messages(messages: list[AnyMessage]) -> list[ToolMessage]:
    """The ToolMessages produced since the last AIMessage — the current super-step's tool
    results. An empty list when the last message is not a tool result batch."""
    trailing: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            trailing.append(message)
        elif isinstance(message, AIMessage):
            break
        else:
            break
    trailing.reverse()
    return trailing


def _scan_parks(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    """Every parked ToolMessage in the current super-step, as
    ``{message_id, tool_call_id, name, interaction_id, expiry_at}``. Empty when no park
    marker is present (the common, non-parking case)."""
    parks: list[dict[str, Any]] = []
    for message in _trailing_tool_messages(messages):
        marker = read_suspended_interaction_marker(message.content)
        if marker is None:
            continue
        if message.id is None:
            # Every checkpointed message has a stable id (LangGraph stamps it before
            # persistence); a marked message with none could not be replaced in place on
            # resume, so refuse loudly rather than substitute the wrong message.
            raise AgentParkMarkerError("a parked ToolMessage has no id to resume against")
        parks.append(
            {
                "message_id": message.id,
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "interaction_id": marker["interaction_id"],
                "expiry_at": marker.get("expiry_at"),
            }
        )
    return parks


class AsyncParkMiddleware(AgentMiddleware):
    """Interrupt the loop once for a super-step's async-ask parks, and substitute their
    answers back on resume."""

    name: str = "AsyncParkMiddleware"

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return _park_or_resume(state["messages"])

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return _park_or_resume(state["messages"])


def _park_or_resume(messages: list[AnyMessage]) -> dict[str, Any] | None:
    """The pure core both hook faces share.

    Park pass: if the super-step has any park markers, ``interrupt`` ONCE with the full
    ``{interaction_id: expiry}`` map for all M suspended siblings, and never returns.

    Resume pass: ``interrupt`` returns the ``{interaction_id: answer}`` map; rewrite each
    marked ToolMessage's content to its answer (same message id → the messages reducer
    replaces it in place). A marker whose interaction id is absent from the answers map is
    a partial resume and raises loudly — never a silent substitution."""
    parks = _scan_parks(messages)
    if not parks:
        return None

    payload = {AGENT_PARK_PAYLOAD_KEY: {"interactions": {p["interaction_id"]: p["expiry_at"] for p in parks}}}
    answers = interrupt(payload)

    if not isinstance(answers, dict):
        raise AgentParkMarkerError(
            f"agent park resume expected an {{interaction_id: answer}} map, got {type(answers).__name__}"
        )
    rewritten: list[ToolMessage] = []
    for park in parks:
        interaction_id = park["interaction_id"]
        if interaction_id not in answers:
            raise AgentParkMarkerError(f"agent park resume is missing the answer for interaction {interaction_id!r}")
        rewritten.append(
            ToolMessage(
                id=park["message_id"],
                content=cast("str | list[str | dict[Any, Any]]", msg_content_output(answers[interaction_id])),
                tool_call_id=park["tool_call_id"],
                name=park["name"],
            )
        )
    return {"messages": rewritten}
