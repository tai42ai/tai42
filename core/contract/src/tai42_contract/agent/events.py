"""Neutral typed event vocabulary for the :class:`Agent` contract.

These are the events an agent's ``astream`` yields and ``run`` drains. They are
provider-agnostic and carry NO langgraph / langchain type — only plain pydantic
models. The producers that build them (the LangGraph ``updates``/``messages``
projection) live in the agents runtime; this module owns only the shapes so
every consumer shares one definition.

``arbitrary_types_allowed`` is set because the event payload fields (``result``,
``data``, ``payload``) hold live objects at runtime — a ``ToolMessage.content``,
a validated pydantic ``response_format`` instance — that are not constrained to
a declared schema. The events are a transport, not a validation boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class StreamEvent(BaseModel):
    """Base class for every event an :class:`Agent` streams. ``type`` is a stable
    discriminator; ``final`` marks a terminal event (see the terminal rule in
    :meth:`Agent._drain`)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    type: str
    final: bool = False


class ReasoningStep(StreamEvent):
    """A chunk of the model's intermediate reasoning ("thinking") for one step
    of the agent loop. Empty/whitespace-only reasoning is never emitted."""

    type: Literal["reasoning_step"] = "reasoning_step"  # pyright: ignore[reportIncompatibleVariableOverride]
    text: str


class ToolCallStep(StreamEvent):
    """One tool invocation the agent decided to make. ``call_id`` is the
    model-assigned id the matching :class:`ToolResultStep` carries."""

    type: Literal["tool_call_step"] = "tool_call_step"  # pyright: ignore[reportIncompatibleVariableOverride]
    tool: str
    args: dict[str, Any]
    call_id: str


class ToolResultStep(StreamEvent):
    """The value one tool call returned, matched to a :class:`ToolCallStep` by
    ``call_id``. ``result`` is passed through untouched."""

    type: Literal["tool_result_step"] = "tool_result_step"  # pyright: ignore[reportIncompatibleVariableOverride]
    tool: str
    call_id: str
    result: Any = None
    is_error: bool = False


class MessageDelta(StreamEvent):
    """A token-level chunk of the agent's final answer as it streams."""

    type: Literal["message_delta"] = "message_delta"  # pyright: ignore[reportIncompatibleVariableOverride]
    text: str


class MessageFinal(StreamEvent):
    """The agent's complete final answer, assembled from the deltas. Terminal."""

    type: Literal["message_final"] = "message_final"  # pyright: ignore[reportIncompatibleVariableOverride]
    text: str
    final: bool = True


class RunUsage(StreamEvent):
    """Token usage (and model label) for the run. Any field may be ``None``."""

    type: Literal["run_usage"] = "run_usage"  # pyright: ignore[reportIncompatibleVariableOverride]
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    model: str | None = None


class StructuredFinal(StreamEvent):
    """The agent's structured (non-text) output. ``data`` is the object the
    agent produced — typically the validated ``response_format`` instance. A
    plain-text answer is carried by :class:`MessageFinal` instead. Terminal."""

    type: Literal["structured_final"] = "structured_final"  # pyright: ignore[reportIncompatibleVariableOverride]
    data: Any = None
    final: bool = True


class SuspendedFinal(StreamEvent):
    """A generic async park surfaced out of a paused agent graph. Terminal.

    The run parked on one or more async ``ask_user`` questions and will resume out
    of band when their answers (or expiries) arrive — it did NOT fail and did NOT
    stop for a live decision, so a non-streaming drain returns a suspended RECEIPT
    for it rather than raising :class:`~tai42_contract.agent.base.AgentInterruptedError`.

    ``interaction_ids`` are the parked questions the resume converges on;
    ``expiry_at`` is the earliest park deadline (ISO-8601, or ``None`` when no park
    carried one); ``thread_id`` is the parked run's thread. The vocabulary is
    generic — it names no driver, engine, or resume state, only the parked
    interaction ids the flow-blind platform already holds."""

    type: Literal["suspended_final"] = "suspended_final"  # pyright: ignore[reportIncompatibleVariableOverride]
    interaction_ids: list[str]
    thread_id: str
    expiry_at: str | None = None
    final: bool = True


class InterruptFinal(StreamEvent):
    """A platform interrupt surfaced out of a paused agent graph. Terminal.

    ``interrupt_id`` is the graph interrupt id (echoed back when resuming);
    ``payload`` is the interrupt value the agent raised (e.g. the option set);
    ``reason`` is an optional human-facing label. This event is emitted verbatim
    as one SSE frame (``type: "interrupt_final"``); a consumer reads
    ``interrupt_id``, ``payload`` and ``reason`` off it — do NOT rename ``payload``
    or drop ``reason``."""

    type: Literal["interrupt_final"] = "interrupt_final"  # pyright: ignore[reportIncompatibleVariableOverride]
    interrupt_id: str
    payload: Any = None
    reason: str | None = None
    final: bool = True
