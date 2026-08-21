"""The hidden ``agent_resume`` continuation tool.

Registered whenever the shared park machinery loads, so ANY park-capable agent module
(deep_agent, tools_agent, …) registers it exactly once by importing the park package. It
is the driver continuation a flow-blind platform fires when an async ``ask_user`` a
park-capable run parked on is answered (or expires): it carries only the generic
``{interaction_id, answer}``. The agents plugin's own durable park index reverses the
interaction id to its parked run and drives auto-pilot to completion or the next park.

Registered ``force=True`` (a mandatory mechanism, never an operator-excludable catalog
tool) and ``tai42/hidden`` (never offered to a model as a callable tool). Delivery is
at-least-once, so the resume is idempotent — the barrier's HSETNX-buffered answers and
the resolved tombstones a completed super-step leaves behind make a redelivered
answer a benign no-op.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_agents._internal.park.driver import AGENT_RESUME_TOOL_NAME, agent_resume


@tai42_app.tools.tool(name=AGENT_RESUME_TOOL_NAME, tags={"agents"}, meta={"tai42/hidden": True}, force=True)
async def agent_resume_tool(interaction_id: str, answer: Any) -> Any:
    """Resume an async-parked agent run from an answered ask_user interaction.

    This is the driver continuation the platform invokes when an async ask_user is
    answered or expires; it carries only the generic {interaction_id, answer}. The agents
    plugin reverses the interaction id to its parked run through its own durable park
    index, buffers the answer into the run's super-step barrier, and — when the last
    sibling answer lands — drives the paused graph to completion or the next park. An
    answer equal to the expiry marker feeds the awaiting tool result its expiry value.

    Args:
        interaction_id: The parked interaction to resume.
        answer: The answer value (or the expiry marker) fed to the awaiting tool result.

    Returns:
        The resumed run's outcome: the final result on completion, a 'buffered' receipt
        while sibling answers are outstanding, or a 'suspended' receipt if the run parked
        again on a further async ask_user.
    """
    return await agent_resume(interaction_id, answer)
