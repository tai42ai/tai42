"""A caller-ask probe agent — the target a conversation route drives through its door contract
(``start_expr`` / ``cancel_expr`` / ``resume_expr`` / ``extras_expr`` / ``reply_expr``).

Unlike ``e2e_park_agent`` (whose baked tool asks the USER, an out-of-band park), this agent's
baked ``e2e_caller_ask`` (registered in ``tai42_e2e_fixtures.tools.door_probe``) asks the run's
CALLER: the run parks a ``to="caller"`` ask the route surfaces through ``reply_expr`` (``$asks``)
and later resumes through ``resume_expr``.

The park machinery is the production one: the agent delegates every run to the real
``tools_agent`` with the caller-ask tool baked in (a conversation agent target is invoked with no
``tool_names``, so a bare ``tools_agent`` route could never reach the tool).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel, Field
from tai42_contract.agent import Agent
from tai42_contract.agent.events import StreamEvent
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText

AGENT_NAME = "e2e_door_agent"

# The one tool the caller-ask agent carries: an async ask addressed to the run's CALLER.
BAKED_TOOL_NAMES = ["e2e_caller_ask"]


class _DoorAgentInput(BaseModel):
    """The run face — the kwargs the SSE agent-run door validates a body against and hands the run.

    ``user_message`` is a :class:`TemplatedText` (the delegate ``tools_agent`` renders it), so an SSE
    body names it as ``{"user_message": {"content": "..."}}``.
    """

    user_message: TemplatedText | None = Field(default=None, description="The message for this run.")
    thread_id: str | None = Field(default=None, description="The conversation thread the run uses.")


@tai42_app.agents.agent(AGENT_NAME, tags={"e2e"})
class E2eDoorAgent(Agent):
    """A ``tools_agent`` carrying :data:`BAKED_TOOL_NAMES`, registered under its own name so a
    conversation route can target it and reach the caller-ask tool."""

    tool_name: ClassVar[str] = AGENT_NAME
    tool_description: ClassVar[str] = (
        "E2E probe agent: a tools_agent carrying the async caller-ask probe tool, so a conversation "
        "agent-route turn can park a caller ask surfaced through reply_expr and resumed through resume_expr."
    )
    ToolInput: ClassVar[type[BaseModel]] = _DoorAgentInput

    async def run(self, **kwargs: Any) -> Any:
        return await self._delegate().run(tool_names=list(BAKED_TOOL_NAMES), **kwargs)

    async def astream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async for event in self._delegate().astream(tool_names=list(BAKED_TOOL_NAMES), **kwargs):
            yield event

    @staticmethod
    def _delegate() -> Agent:
        """The real ``tools_agent`` instance, resolved per call so a reload is picked up."""
        return tai42_app.agents.get_agent("tools_agent")


@tai42_app.agents.agent("e2e_extras_agent", tags={"e2e"})
class E2eExtrasAgent(Agent):
    """A ``tools_agent`` that DECLARES the door extras it reads (``extras_keys``) and records them.

    A door's ``extras_expr`` builds the extras a route/hook/schedule hands the started run; the visit
    admits only the keys a target declares. This agent declares ``{"tag"}`` and, at the start of its
    stream, reads the ambient run extras and RPUSHes them onto ``e2e:rec:agent_extras:{tag}`` — so a
    spec proves the ``extras_expr`` result reached the agent run. An ``extras_expr`` naming a key it
    does NOT declare is refused loudly by the visit before the run starts.
    """

    tool_name: ClassVar[str] = "e2e_extras_agent"
    tool_description: ClassVar[str] = "E2E probe agent that declares and records the door extras it reads."
    ToolInput: ClassVar[type[BaseModel]] = _DoorAgentInput
    extras_keys: ClassVar[frozenset[str]] = frozenset({"tag"})

    async def run(self, **kwargs: Any) -> Any:
        await self._record_extras()
        return await self._delegate().run(**kwargs)

    async def astream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        await self._record_extras()
        async for event in self._delegate().astream(**kwargs):
            yield event

    @staticmethod
    async def _record_extras() -> None:
        import json
        from collections.abc import Awaitable
        from typing import cast

        from tai42_contract.tools import current_extras
        from tai42_kit.clients import client_ctx
        from tai42_kit.clients.impl.redis import RedisClient

        from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings

        extras = dict(current_extras())
        tag = extras.get("tag")
        if tag is None:
            return
        async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
            await cast(Awaitable[int], client.rpush(f"e2e:rec:agent_extras:{tag}", json.dumps(extras)))

    @staticmethod
    def _delegate() -> Agent:
        """The real ``tools_agent`` instance, resolved per call so a reload is picked up."""
        return tai42_app.agents.get_agent("tools_agent")
