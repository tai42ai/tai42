"""Agent contract: the :class:`Agent` ABC + its neutral streamed-event vocabulary.

Re-exports the ``Agent`` interface (and its ``PresetSpec`` / ``SubAgentSpec``
inputs and ``AgentInterruptedError`` error) alongside the ``StreamEvent`` types an
agent's ``astream`` yields and ``run`` drains.
"""

from __future__ import annotations

from tai42_contract.agent.base import (
    Agent,
    AgentInterruptedError,
    PresetSpec,
    SubAgentSpec,
)
from tai42_contract.agent.events import (
    InterruptFinal,
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    StreamEvent,
    StructuredFinal,
    SuspendedFinal,
    ToolCallStep,
    ToolResultStep,
)

# The ambient in-process session-thread door: a logic-free channel both the skeleton
# run-tool binding (reader) and a contract-facing plugin (writer) reach through the one
# package they share.
from tai42_contract.agent.session_thread import (
    agent_session_thread,
    get_agent_session_thread,
    reset_agent_session_thread,
    set_agent_session_thread,
)

__all__ = [
    "Agent",
    "AgentInterruptedError",
    "InterruptFinal",
    "MessageDelta",
    "MessageFinal",
    "PresetSpec",
    "ReasoningStep",
    "RunUsage",
    "StreamEvent",
    "StructuredFinal",
    "SubAgentSpec",
    "SuspendedFinal",
    "ToolCallStep",
    "ToolResultStep",
    "agent_session_thread",
    "get_agent_session_thread",
    "reset_agent_session_thread",
    "set_agent_session_thread",
]
