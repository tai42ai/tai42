"""The hidden ``agent_resume`` continuation tool and its idempotent registration.

``register_agent_resume_tool()`` binds the ``agent_resume`` driver continuation a
flow-blind platform fires when an async ``ask_user`` a park-capable run parked on is
answered (or expires): it carries only the generic ``{interaction_id, answer}``. The
agents plugin's own durable park index reverses the interaction id to its parked run and
drives auto-pilot to completion or the next park.

EVERY parking agent calls this from ITS OWN registration (``claude_code``,
``langchain_deep_agent``, ``tools_agent``) — there is no shared module-import site that
would fire it exactly once, and a MODULE-IMPORT side effect (the old shape) would starve
every post-boot reload epoch of its binding (kit/plugin modules are import-cached, not
re-imported on reload). Per-epoch IDEMPOTENCE makes the multiple callers safe: the first
call binds, later ones catch the FastMCP duplicate-bind error and no-op, so a box loading
ANY subset of the parking agents — and every reload epoch — ends with EXACTLY one binding.

Registered ``force=True`` (a mandatory mechanism, never an operator-excludable catalog
tool) and ``tai42/hidden`` (never offered to a model as a callable tool). Delivery is
at-least-once, so the resume is idempotent — the barrier's HSETNX-buffered answers and
the resolved tombstones a completed super-step leaves behind make a redelivered answer a
benign no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.app import tai42_app

from tai42_agents._internal.park.driver import AGENT_RESUME_TOOL_NAME, agent_resume

logger = logging.getLogger(__name__)


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


def register_agent_resume_tool() -> None:
    """Idempotently bind the hidden ``agent_resume`` continuation tool.

    Called from every parking agent's registration. The first call binds; a later call on
    the same epoch re-attempts the bind and catches the FastMCP duplicate-bind error
    (``ValueError('Component already exists: ...')``), debug-logging the no-op — NOT a
    process-lifetime flag (that would starve every post-boot reload epoch of its binding,
    since the module is import-cached and not re-imported on reload). Any OTHER error
    propagates loudly so a genuine registration bug is never swallowed."""
    try:
        tai42_app.tools.tool(
            name=AGENT_RESUME_TOOL_NAME,
            tags={"agents"},
            meta={"tai42/hidden": True},
            force=True,
        )(agent_resume_tool)
    except ValueError as exc:
        if "already exists" not in str(exc):
            raise
        logger.debug("agent_resume tool already bound this epoch; registration is a no-op")
