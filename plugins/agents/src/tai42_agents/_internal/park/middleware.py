"""The ``before_model`` park hook for a park-capable agent loop.

A side-effect-free middleware placed at the head of the agent loop (both the tools-agent
and deep-agent loops mount it). Its whole job is to turn the reserved async-park marker
(stamped onto a ToolMessage by the in-process client-tool seam when an async ``ask``
returned its sentinel) into a single graph ``interrupt`` for the super-step, and — on
resume — to substitute each parked ToolMessage's answer back in place.

This is also the CLAIM POINT for a park: the interrupt here is what leads to this run
recording durable resume state against those interactions, so ownership is checked here
(:func:`~tai42_contract.interactions.assert_park_adoptable`) and not only where a sentinel
object crossed a tool face. A park reaches this hook as tool-result CONTENT, so the check
holds even when the object seams were never on the path — a middle agent that could not park
hands its caller the raw marker. The ownership check is a NESTING discriminator (it refuses a
park that crossed a nested run's face, which carries no owner), NOT an authenticity token: the
resume owner is a public continuation name, so it does not by itself defend against content a
model shaped to name the bound continuation. What this hook does defend is shape — a malformed
"marker" is never read as a park (:func:`~tai42_contract.interactions.read_suspended_interaction_marker`
returns ``None`` for one naming no interaction and raises for one lacking its id lists, which is
answered with a model-visible error) — and this before-model hook holds no interactions-store handle, so
store-authoritative existence checks are not applied here.

On resume, an answer may itself say the awaited outcome FAILED (a chained call whose nested
run ended without a result): that answer shape is defined here and substituted as the same
model-visible error ToolMessage a refused park takes, never as a value the model would read
as the tool's answer.

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

import logging
from typing import Any, Final, cast

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.prebuilt.tool_node import msg_content_output
from langgraph.types import interrupt
from tai42_contract.interactions import (
    MalformedParkMarkerError,
    NestedParkOwnershipError,
    assert_park_adoptable,
    read_suspended_interaction_marker,
)

from tai42_agents._internal.park.errors import AgentParkMarkerError

logger = logging.getLogger(__name__)

# The reserved key a park interrupt's VALUE carries, so the drive wrapper recognizes a
# park interrupt by its value shape (never a name). Internal to the agents plugin — the
# platform contract knows only the generic ToolMessage marker, not this interrupt payload.
AGENT_PARK_PAYLOAD_KEY: Final[str] = "tai42:agent_park"

# The reserved shape a resumed park's ANSWER may carry instead of a value, to say the awaited
# outcome did not arrive. A chained park waits on a nested run's terminal, and that terminal can
# be a failure, so the resume has to be able to say so — an answer is otherwise substituted
# verbatim as the tool's result, which would hand the model a failure dressed as an answer (or,
# for an empty one, silence). Internal to the agents plugin: the delivery tool that maps a
# non-success terminal builds it, and the substitution below turns it back into a model-visible
# error ToolMessage.
PARK_ANSWER_ERROR_KEY: Final[str] = "tai42:agent_park_error"


def park_error_answer(detail: str) -> dict[str, str]:
    """The answer value that resumes a park with a FAILURE.

    ``detail`` is what the model reads as the tool's error.
    """
    return {PARK_ANSWER_ERROR_KEY: detail}


def read_park_error_answer(answer: Any) -> str | None:
    """The failure detail an answer carries, or ``None`` for an ordinary answer.

    An ordinary answer is every value a human or an expiry marker delivers.
    """
    if isinstance(answer, dict) and PARK_ANSWER_ERROR_KEY in answer:
        detail = answer[PARK_ANSWER_ERROR_KEY]
        return detail if isinstance(detail, str) else str(detail)
    return None


def _trailing_tool_messages(messages: list[AnyMessage]) -> list[ToolMessage]:
    """The ToolMessages produced since the last AIMessage — the current super-step's tool results.

    An empty list when the last message is not a tool result batch.
    """
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


def _scan_parks(messages: list[AnyMessage]) -> tuple[list[dict[str, Any]], list[ToolMessage]]:
    """Every parked ToolMessage in the current super-step as a marker dict, and refusals of malformed ones.

    Each entry is ``{message_id, tool_call_id, name, interaction_id, expiry_at, resume_owner,
    caller_interaction_ids}``. Nothing here decides adoptability; it only surfaces what the claim
    check needs. A ToolMessage whose content names an interaction under the reserved key but lacks
    a valid id list (:class:`~tai42_contract.interactions.MalformedParkMarkerError`) is never a
    park: it is logged and answered with a model-visible error ToolMessage, the treatment a refused
    park takes — tool-result content a model can shape must not abort the run.
    """
    parks: list[dict[str, Any]] = []
    refusals: list[ToolMessage] = []
    for message in _trailing_tool_messages(messages):
        try:
            marker = read_suspended_interaction_marker(message.content)
        except MalformedParkMarkerError as exc:
            logger.exception("refusing a malformed park marker on tool call %s", message.tool_call_id)
            refusals.append(_error_message(_message_slot(message), str(exc)))
            continue
        if marker is None:
            continue
        parks.append(
            {
                **_message_slot(message),
                "interaction_id": marker["interaction_id"],
                "expiry_at": marker.get("expiry_at"),
                # A marker naming no owner (content a model shaped) yields ``None``, which the
                # claim check refuses as a nested/foreign park.
                "resume_owner": marker.get("resume_owner"),
                # The caller subset the ask's marker carries (``[id]`` for a ``to="caller"`` ask,
                # empty for a user ask) — surfaced so the super-step's park receipt partitions
                # caller from user asks for the platform's ``visit`` normalisation.
                "caller_interaction_ids": marker["caller_interaction_ids"],
            }
        )
    return parks, refusals


def _message_slot(message: ToolMessage) -> dict[str, Any]:
    """The identity of a marked ToolMessage a replacement message is written against."""
    if message.id is None:
        # Every checkpointed message has a stable id (LangGraph stamps it before persistence); a
        # marked message with none could not be replaced in place, so refuse loudly rather than
        # substitute the wrong message.
        raise AgentParkMarkerError("a parked ToolMessage has no id to resume against")
    return {"message_id": message.id, "tool_call_id": message.tool_call_id, "name": message.name}


def _error_message(park: dict[str, Any], reason: str) -> ToolMessage:
    """The model-visible error ToolMessage a parked tool result is replaced by.

    Replaces a park this run may not claim, or one whose awaited outcome came back a failure.

    An error RESULT, never a dropped marker: leaving the marker in place would hand the model
    the raw park JSON as if it were the tool's answer, and dropping the message would strand
    the tool_call unanswered. The id is the marked message's, so the reducer replaces it.
    """
    return ToolMessage(
        id=park["message_id"],
        content=reason,
        tool_call_id=park["tool_call_id"],
        name=park["name"],
        status="error",
    )


def _partition_claimable(parks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[ToolMessage]]:
    """Split the super-step's parks into the ones THIS run may claim and error messages for the rest.

    This is the CLAIM POINT: recording resume state against an interaction is what the
    interrupt below leads to, so ownership is checked here — not only where a sentinel object
    crossed a tool face. A park reaches this scan as wire content, so a run that never saw the
    sentinel (a non-park-capable middle agent handed its caller the raw marker) is still
    stopped from claiming a park it does not own.
    """
    claimable: list[dict[str, Any]] = []
    refusals: list[ToolMessage] = []
    for park in parks:
        try:
            assert_park_adoptable(
                park["resume_owner"], interaction_id=park["interaction_id"], tool_name=park["name"] or "<unnamed>"
            )
        except NestedParkOwnershipError as exc:
            refusals.append(_error_message(park, str(exc)))
        else:
            claimable.append(park)
    return claimable, refusals


class AsyncParkMiddleware(AgentMiddleware):
    """Interrupt the loop once for a super-step's async-ask parks, and substitute their answers back on resume."""

    name: str = "AsyncParkMiddleware"

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return _park_or_resume(state["messages"])

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return _park_or_resume(state["messages"])


def _park_or_resume(messages: list[AnyMessage]) -> dict[str, Any] | None:
    """The pure core both hook faces share.

    Claim check: every park marker in the super-step is checked against this run's bound
    resume continuation. One it may not claim never enters the interrupt — it is replaced by a
    model-visible error ToolMessage, so the model reads the refusal and the turn continues.
    With NO claimable park left the hook returns those refusals and does not interrupt at all.

    Park pass: ``interrupt`` ONCE with the ``{interaction_id: expiry}`` map for the claimable
    siblings, and never returns.

    Resume pass: ``interrupt`` returns the ``{interaction_id: answer}`` map; rewrite each
    claimed ToolMessage's content to its answer (same message id → the messages reducer
    replaces it in place), alongside any refusals of the same super-step. A marker whose
    interaction id is absent from the answers map is a partial resume and raises loudly —
    never a silent substitution.
    """
    parks, malformed = _scan_parks(messages)
    if not parks:
        return {"messages": malformed} if malformed else None

    claimable, refusals = _partition_claimable(parks)
    refusals = malformed + refusals
    if not claimable:
        # Nothing of this super-step is this run's to park on. Answer the tool_calls with the
        # refusals and let the loop run on — never interrupt on another run's park. The drive
        # finalizer's read-skip (``finalize_drive``) leans on this: a run that cannot host a
        # park produces a model-visible refusal here instead of a park interrupt, so there is
        # no pending park interrupt for the finalizer to have to classify.
        return {"messages": refusals}

    # The caller subset of the claimable super-step: an ask whose own id is in its marker's
    # caller subset is a ``to="caller"`` ask. Carried on the interrupt payload so the drive
    # finalizer can partition the park receipt.
    caller_ids = [p["interaction_id"] for p in claimable if p["interaction_id"] in p["caller_interaction_ids"]]
    payload = {
        AGENT_PARK_PAYLOAD_KEY: {
            "interactions": {p["interaction_id"]: p["expiry_at"] for p in claimable},
            "caller_interaction_ids": caller_ids,
        }
    }
    answers = interrupt(payload)

    if not isinstance(answers, dict):
        raise AgentParkMarkerError(
            f"agent park resume expected an {{interaction_id: answer}} map, got {type(answers).__name__}"
        )
    rewritten: list[ToolMessage] = []
    for park in claimable:
        interaction_id = park["interaction_id"]
        if interaction_id not in answers:
            raise AgentParkMarkerError(f"agent park resume is missing the answer for interaction {interaction_id!r}")
        answer = answers[interaction_id]
        failure = read_park_error_answer(answer)
        if failure is not None:
            # The awaited outcome did not arrive (a chained call whose nested run ended without
            # a result). It takes the SAME model-visible error ToolMessage a refused park does —
            # never a silent empty result, and never the failure serialized as if it were the
            # answer.
            rewritten.append(_error_message(park, failure))
            continue
        rewritten.append(
            ToolMessage(
                id=park["message_id"],
                content=cast("str | list[str | dict[Any, Any]]", msg_content_output(answer)),
                tool_call_id=park["tool_call_id"],
                name=park["name"],
            )
        )
    # The refusals ride the SAME update as the answers: a super-step mixing a park this run
    # owns with one it does not resolves both in one step, so the model never meets a raw
    # marker as a tool result.
    return {"messages": rewritten + refusals}
