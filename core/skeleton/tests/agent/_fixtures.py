"""Agent fixtures for the run-tool synthesis tests.

``EchoFieldsAgent.run`` echoes the sorted names of the kwargs it actually
received, so a test can observe that the synthesized run tool forwards only the
caller-supplied fields (``from_tool_input``'s set-fields-only contract) rather
than every field materialized with its default.

``NestedToolsAgent.run`` resolves its own tools BY NAME from the process-global
tool facet mid-turn and invokes one — the resolution a real agent (and any
subagent it spawns) performs for itself, which no wrapping at the agent's own
call site can reach. It is how a test observes what the shared tool-dispatch seam
does to a tool an agent picked up on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from tai42_contract.agent import AddressedMemory, Agent
from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction
from tests.agent._memoryless import MemorylessTestAgent


class EchoInput(BaseModel):
    text: str
    times: int = 1
    note: str = "unset"


@tai42_app.agents.agent("echo_fields")
class EchoFieldsAgent(MemorylessTestAgent):
    tool_name = "echo_fields"
    tool_description = "Echo which fields were forwarded."
    ToolInput = EchoInput

    async def run(self, **kwargs) -> str:
        return ",".join(sorted(kwargs))


class NullDefaultInput(BaseModel):
    """A ``ToolInput`` mixing a required field, a ``null``-default optional field, and a
    NON-``null``-default nullable field, so a test can observe how the run tool treats an
    explicit ``None`` per default kind: dropped for a ``null`` default (a transformed-tool
    caller fills an omitted argument with the schema default, so ``None`` is indistinguishable
    from omission), kept for a non-``null`` default (there ``None`` is a real caller-chosen
    value distinct from the default)."""

    text: str
    optional_null: str | None = None
    optional_valued: str | None = "seed"


@tai42_app.agents.agent("null_default_fields")
class NullDefaultFieldsAgent(MemorylessTestAgent):
    tool_name = "null_default_fields"
    tool_description = "Echo which fields survived as set fields."
    ToolInput = NullDefaultInput

    async def run(self, **kwargs) -> str:
        return ",".join(sorted(kwargs))


# The contract ``AppAgents.agent`` carries the generic ``meta`` passthrough, so a
# meta-carrying registration goes through the plain ``tai42_app.agents`` protocol surface.
@tai42_app.agents.agent("meta_carrier", meta={"tai42/crash_resume": True})
class MetaCarrierAgent(MemorylessTestAgent):
    """Registers with generic ``meta`` so the run-tool ``FunctionTool`` carries it —
    exercising the generic registration passthrough (naming no consumer concept)."""

    tool_name = "meta_carrier"
    tool_description = "An agent whose run tool carries generic registration meta."
    ToolInput = EchoInput

    async def run(self, **kwargs) -> str:
        return "ok"


class PresetSpecLike(BaseModel):
    base_tool: str
    fixed_kwargs: dict[str, Any] = {}


class SubAgentSpecLike(BaseModel):
    name: str
    prompt: str = ""


class InlineSkillLike(BaseModel):
    name: str
    content: str


class NestedInput(BaseModel):
    """A ``ToolInput`` carrying nested pydantic-model fields, standing in for the
    real ``tools_agent`` / ``langchain_deep_agent`` inputs (the skeleton binds the ``Agent``
    contract only and never imports ``tai42_agents``).

    ``presets`` / ``subagents`` / ``inline_skills`` each nest a model, so
    ``model_json_schema`` emits them as ``$defs`` refs — the shape an extension
    branch must preserve when it composes over the synthesized run tool."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="A required scalar field.")
    times: int = 1
    presets: list[PresetSpecLike] | None = Field(default=None, description="Nested preset specs.")
    subagents: list[SubAgentSpecLike] | None = None
    inline_skills: list[InlineSkillLike] | None = None


@tai42_app.agents.agent("nested_fields")
class NestedFieldsAgent(MemorylessTestAgent):
    tool_name = "nested_fields"
    tool_description = "Echo which nested fields were forwarded."
    ToolInput = NestedInput

    async def run(self, **kwargs) -> str:
        return ",".join(sorted(kwargs))


asker_run_called = False


class AskerInput(BaseModel):
    user_message: str = ""
    tool_names: list[str] = Field(default_factory=list)


@tai42_app.agents.agent("asker_agent")
class AskerAgent(MemorylessTestAgent):
    """An agent whose per-call tool set may include the caller-ask tool, with a flag recording whether
    ``run`` was reached — so a test can observe the run-tool face REFUSE (``run`` never called) when
    the door declared no caller-ask landing and the exact tool set binds ``ask``."""

    tool_name = "asker_agent"
    tool_description = "Resolve tools per call; record whether run was reached."
    ToolInput = AskerInput

    async def run(self, **kwargs) -> Any:
        global asker_run_called
        asker_run_called = True
        return "ran"


class ParkInput(BaseModel):
    text: str = ""


@tai42_app.agents.agent("parking")
class ParkingAgent(MemorylessTestAgent):
    """A run that parks on an async ask: its ``run`` returns a ``SuspendedInteraction`` typed
    value (the shape ``Agent._drain`` yields), so a test can observe the agent tool-face pass the
    park through by TYPE — a caller recognizes the park without any status inspection."""

    tool_name = "parking"
    tool_description = "Park on an async ask and return the suspended sentinel."
    ToolInput = ParkInput
    parks = True

    async def run(self, **kwargs) -> Any:
        return SuspendedInteraction(
            interaction_id="i-parked",
            interaction_ids=["i-parked"],
            caller_interaction_ids=[],
        )


class StatusAnswerInput(BaseModel):
    text: str = ""


@tai42_app.agents.agent("status_answer")
class StatusAnswerAgent(MemorylessTestAgent):
    """A run whose own ANSWER is a plain dict that happens to carry a ``status`` key equal to
    ``"suspended"``. The run did NOT park — the dict is its result — so the agent tool-face must
    deliver it as a SUCCESS, recognising a park by TYPE (``SuspendedInteraction``) and never by a
    status key in a returned value."""

    tool_name = "status_answer"
    tool_description = "Answer with a plain dict that carries a status key."
    ToolInput = StatusAnswerInput

    async def run(self, **kwargs) -> Any:
        return {"status": "suspended", "data": "this is the real answer"}


class NestedToolsInput(BaseModel):
    """The tool an agent resolves for itself mid-turn, plus the arguments it invokes
    it with."""

    tool_name: str
    arguments: dict[str, Any] = {}


@tai42_app.agents.agent("nested_tools")
class NestedToolsAgent(MemorylessTestAgent):
    tool_name = "nested_tools"
    tool_description = "Resolve one tool by name mid-turn and invoke it."
    ToolInput = NestedToolsInput

    async def run(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Resolve ``tool_name`` from the process-global facet DURING the turn and
        invoke it. Nothing is captured up front, so the only place a decision can reach
        this call is the shared dispatch seam."""
        [tool] = await tai42_app.tools.get_client_tools([tool_name])
        return await tool.invoke(arguments or {})


class SleeperInput(BaseModel):
    """Drives ``SleeperAgent`` to sleep ``seconds`` before returning — the slow-turn
    stand-in for the run-timeout tests."""

    seconds: float = 0.0


# Set to True only if a ``SleeperAgent`` run reaches its end; a run cancelled by the
# turn timeout must leave it False (no orphaned turn continuing past the deadline).
sleeper_completed = False


@tai42_app.agents.agent("sleeper")
class SleeperAgent(MemorylessTestAgent):
    tool_name = "sleeper"
    tool_description = "Sleep, then report completion."
    ToolInput = SleeperInput

    async def run(self, seconds: float = 0.0, **kwargs) -> str:
        global sleeper_completed
        await asyncio.sleep(seconds)
        sleeper_completed = True
        return "done"


@tai42_app.agents.agent("inner_timeout")
class InnerTimeoutAgent(MemorylessTestAgent):
    """A run whose own work raises a builtin ``TimeoutError`` (standing in for an
    inner budget abort such as the jq utility's). Under a generous turn timeout the
    caller must receive this ORIGINAL error, never a ``TurnTimeoutError``."""

    tool_name = "inner_timeout"
    tool_description = "Raise a builtin TimeoutError from inside the turn."
    ToolInput = SleeperInput

    async def run(self, seconds: float = 0.0, **kwargs) -> str:
        raise TimeoutError("inner budget exceeded")


def _scope_memory(options: Any) -> AddressedMemory:
    """The thread and checkpoint ids an ``engine_options`` blob carries under its ``scope``."""
    scope = options.get("scope") if isinstance(options, dict) else None
    if not isinstance(scope, dict):
        return AddressedMemory()
    threads = frozenset({scope["thread"]}) if isinstance(scope.get("thread"), str) else frozenset()
    checkpoints = frozenset({scope["checkpoint"]}) if isinstance(scope.get("checkpoint"), str) else frozenset()
    return AddressedMemory(thread_ids=threads, checkpoint_ids=checkpoints)


class ConfigInput(BaseModel):
    """A ``ToolInput`` carrying an engine option blob — a thread-scoping vector only the agent
    itself can read, reported to the platform through ``addressed_memory``."""

    text: str
    engine_options: dict[str, Any] | None = None


@tai42_app.agents.agent("config_fields")
class ConfigFieldsAgent(Agent):
    tool_name = "config_fields"
    tool_description = "Echo the thread id its option blob carries."
    ToolInput = ConfigInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        return _scope_memory(run_kwargs.get("engine_options"))

    async def run(self, **kwargs) -> str:
        options = kwargs.get("engine_options") or {}
        return str(options.get("scope", {}).get("thread", ""))


# Records the ``thread_id`` each ``ThreadRecorderAgent`` run actually received, so a test
# can observe whether the synthesized run tool injected the ambient session thread (deposited
# out-of-band) and that a caller-pinned ``thread_id`` still wins over the deposit.
thread_recorder_seen: list[str | None] = []


class ThreadInput(BaseModel):
    """A ``ToolInput`` whose optional ``thread_id`` lets a caller pin the run thread
    directly, standing in for an agent input that already carries one. ``resume_checkpoint_id``
    is a checkpoint the run continues from, and the optional ``engine_options`` blob carries a
    thread pin of the agent's own spelling (``scope.thread``); the agent reports both through
    ``addressed_memory``, so the ambient-deposit gate yields to either pin."""

    text: str
    thread_id: str | None = None
    resume_checkpoint_id: str | None = None
    engine_options: dict | None = None


@tai42_app.agents.agent("thread_recorder")
class ThreadRecorderAgent(Agent):
    tool_name = "thread_recorder"
    tool_description = "Record the thread id the run received."
    ToolInput = ThreadInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        memory = _scope_memory(run_kwargs.get("engine_options"))
        checkpoint = run_kwargs.get("resume_checkpoint_id")
        if isinstance(checkpoint, str):
            memory = memory.model_copy(
                update={
                    "keys": frozenset({"resume_checkpoint_id"}),
                    "checkpoint_ids": memory.checkpoint_ids | {checkpoint},
                }
            )
        return memory

    async def run(self, **kwargs) -> str:
        # ``from_tool_input`` forwards set fields only, so ``thread_id`` arrives here ONLY
        # when the caller pinned it OR the run tool injected the ambient session thread.
        thread_id = kwargs.get("thread_id")
        thread_recorder_seen.append(thread_id)
        return thread_id or ""
