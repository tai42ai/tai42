"""The ``before_model`` park hook for a park-capable agent loop.

A side-effect-free middleware placed at the head of the agent loop (both the tools-agent
and deep-agent loops mount it). Its whole job is to turn the reserved async-park marker
(stamped onto a ToolMessage by the in-process client-tool seam when an async ``ask_user``
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
"marker" is not read as a park at all (:func:`~tai42_contract.interactions.read_suspended_interaction_marker`
returns ``None``) — and this before-model hook holds no interactions-store handle, so
store-authoritative existence checks are not applied here.

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

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any, Final, cast

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.prebuilt.tool_node import msg_content_output
from langgraph.types import interrupt
from tai42_contract.interactions import (
    NestedParkOwnershipError,
    assert_park_adoptable,
    read_suspended_interaction_marker,
)

from tai42_agents._internal.park.errors import AgentParkMarkerError

# The reserved key a park interrupt's VALUE carries, so the drive wrapper recognizes a
# park interrupt by its value shape (never a name). Internal to the agents plugin — the
# platform contract knows only the generic ToolMessage marker, not this interrupt payload.
AGENT_PARK_PAYLOAD_KEY: Final[str] = "tai42:agent_park"


# The interaction ids the driver is resuming into THIS graph right now. The resume driver binds
# it around the ``Command(resume=...)`` drive; the claim check reads it so an in-flight park is
# claimable while it is being resumed even when its wire marker predates the ``resume_owner``
# field. Empty (the default) on an initial park pass and any code outside a driver resume.
_resuming_park_interaction_ids: ContextVar[frozenset[str]] = ContextVar(
    "tai42_resuming_park_interaction_ids", default=frozenset()
)


@contextlib.contextmanager
def resuming_park_interaction_ids(interaction_ids: frozenset[str]) -> Iterator[None]:
    """Bind the interaction ids a driver is resuming into this graph, for the drive's duration.

    The resume driver knows exactly which interactions it is delivering answers for (the
    super-step it drives), so it names them here around the ``Command(resume=...)`` drive. The
    claim check treats a park whose id is in this set as claimable regardless of the marker's
    ``resume_owner``: a resume is fired only for a park THIS run owns, so an in-flight park stays
    resumable even when its persisted wire marker carries no owner (a park written by a released
    predecessor and upgraded mid-flight). Set and reset around the drive."""
    token = _resuming_park_interaction_ids.set(interaction_ids)
    try:
        yield
    finally:
        _resuming_park_interaction_ids.reset(token)


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
    ``{message_id, tool_call_id, name, interaction_id, expiry_at, resume_owner}``. Empty when
    no park marker is present (the common, non-parking case).

    ``resume_owner`` is read off the marker with ``.get``: a marker that carries no such key —
    an older wire form, or content a model shaped without one — yields ``None``, which
    :func:`assert_park_adoptable` refuses as a nested/foreign park. Nothing here decides
    adoptability; it only surfaces what the claim check needs. The marker's SHAPE was already
    validated upstream (:func:`read_suspended_interaction_marker` returns ``None`` for anything
    that is not a well-formed marker), so every entry here carries a real ``interaction_id``."""
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
                "resume_owner": marker.get("resume_owner"),
            }
        )
    return parks


def _refusal_message(park: dict[str, Any], reason: str) -> ToolMessage:
    """The model-visible error ToolMessage a park this run may not claim is replaced by.

    An error RESULT, never a dropped marker: leaving the marker in place would hand the model
    the raw park JSON as if it were the tool's answer, and dropping the message would strand
    the tool_call unanswered. The id is the marked message's, so the reducer replaces it."""
    return ToolMessage(
        id=park["message_id"],
        content=reason,
        tool_call_id=park["tool_call_id"],
        name=park["name"],
        status="error",
    )


def _partition_claimable(parks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[ToolMessage]]:
    """Split the super-step's parks into the ones THIS run may claim and the error messages
    replacing the ones it may not.

    This is the CLAIM POINT: recording resume state against an interaction is what the
    interrupt below leads to, so ownership is checked here — not only where a sentinel object
    crossed a tool face. A park reaches this scan as wire content, so a run that never saw the
    sentinel (a non-park-capable middle agent handed its caller the raw marker) is still
    stopped from claiming a park it does not own.

    An interaction the driver is resuming RIGHT NOW (in :func:`resuming_park_interaction_ids`) is
    claimable regardless of the marker's owner: a resume is fired only for a park this run owns,
    so an in-flight park stays resumable even when its persisted wire marker predates the
    ``resume_owner`` field (a park written by a released predecessor, upgraded mid-flight). Its
    ownerless marker would otherwise be refused here and the operator's delivered answer dropped."""
    resuming = _resuming_park_interaction_ids.get()
    claimable: list[dict[str, Any]] = []
    refusals: list[ToolMessage] = []
    for park in parks:
        if park["interaction_id"] in resuming:
            claimable.append(park)
            continue
        try:
            assert_park_adoptable(
                park["resume_owner"], interaction_id=park["interaction_id"], tool_name=park["name"] or "<unnamed>"
            )
        except NestedParkOwnershipError as exc:
            refusals.append(_refusal_message(park, str(exc)))
        else:
            claimable.append(park)
    return claimable, refusals


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
    never a silent substitution."""
    parks = _scan_parks(messages)
    if not parks:
        return None

    claimable, refusals = _partition_claimable(parks)
    if not claimable:
        # Nothing of this super-step is this run's to park on. Answer the tool_calls with the
        # refusals and let the loop run on — never interrupt on another run's park. The drive
        # finalizer's read-skip (``finalize_drive``) leans on this: a run that cannot host a
        # park produces a model-visible refusal here instead of a park interrupt, so there is
        # no pending park interrupt for the finalizer to have to classify.
        return {"messages": refusals}

    payload = {AGENT_PARK_PAYLOAD_KEY: {"interactions": {p["interaction_id"]: p["expiry_at"] for p in claimable}}}
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
        rewritten.append(
            ToolMessage(
                id=park["message_id"],
                content=cast("str | list[str | dict[Any, Any]]", msg_content_output(answers[interaction_id])),
                tool_call_id=park["tool_call_id"],
                name=park["name"],
            )
        )
    # The refusals ride the SAME update as the answers: a super-step mixing a park this run
    # owns with one it does not resolves both in one step, so the model never meets a raw
    # marker as a tool result.
    return {"messages": rewritten + refusals}
