"""A registered AGENT whose runs can async-park — the target a conversation ``target_kind=agent``
route needs to exercise the agent-direction park/deliver round trip.

The conversation door invokes an agent target as ``astream(user_message=..., thread_id=...)``:
no ``tool_names`` cross that seam, so a bare ``tools_agent`` route can never reach a parking
tool. This agent bakes the tool list and delegates every run to the REAL ``tools_agent``, so the
park machinery under test is the production one — this module adds a registration, not a driver.

The park entry the delegate writes names ``tools_agent`` as its agent, so the out-of-band resume
rebuilds through the delegate's own ``aresume_park`` from the stored ``rebuild_kwargs`` (which
carry the baked tool names). Nothing here participates in the resume.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel, Field
from tai42_contract.agent import Agent
from tai42_contract.agent.events import StreamEvent
from tai42_contract.app import tai42_app

AGENT_NAME = "e2e_park_agent"

# The baked tool list every run carries. ``e2e_agent_async_ask`` is the async ``ask_user`` the
# scripted model calls to park the run; ``e2e_record_identity`` lets a resumed turn record the
# identity it drove under, exactly as the agents park suite reads it.
BAKED_TOOL_NAMES = ["e2e_agent_async_ask", "e2e_record_identity"]


class _ParkAgentInput(BaseModel):
    """The run-tool face. Only the two kwargs a conversation agent target is invoked with."""

    user_message: str = Field(default="", description="The visitor's message for this turn.")
    thread_id: str | None = Field(default=None, description="The conversation thread the turn runs on.")


@tai42_app.agents.agent(AGENT_NAME, tags={"e2e"})
class E2eParkAgent(Agent):
    """A ``tools_agent`` with :data:`BAKED_TOOL_NAMES` baked in, registered under its own name so
    a conversation route can target it."""

    tool_name: ClassVar[str] = AGENT_NAME
    tool_description: ClassVar[str] = (
        "E2E probe agent: a tools_agent carrying the async-ask probe tools, so a conversation "
        "agent-route turn can park on an async ask_user and deliver its resumed answer out of band."
    )
    ToolInput: ClassVar[type[BaseModel]] = _ParkAgentInput

    async def run(self, **kwargs: Any) -> Any:
        return await self._delegate().run(tool_names=list(BAKED_TOOL_NAMES), **kwargs)

    async def astream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        # Straight passthrough — including the ``SuspendedFinal`` a park emits, which is what the
        # conversation door reads to end the turn silently.
        async for event in self._delegate().astream(tool_names=list(BAKED_TOOL_NAMES), **kwargs):
            yield event

    @staticmethod
    def _delegate() -> Agent:
        """The real ``tools_agent`` instance — resolved per call, so a reload that re-registers
        the agent is picked up rather than pinned at import."""
        return tai42_app.agents.get_agent("tools_agent")
