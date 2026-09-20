"""Park-identity assembly for the ``langchain_deep_agent`` faces.

:func:`build_rebuild_kwargs` is the JSON-serializable subset of a run's inputs that
determines graph compilation — the identity a cross-worker resume recompiles from.
:func:`build_astream_park` assembles the streaming face's :class:`ParkIdentity` when
the turn is both completion-bound and rebuildable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.tools import StructuredTool
from tai42_contract.agent.base import SubAgentSpec as NeutralSubAgentSpec
from tai42_contract.interactions import get_park_completion

from tai42_agents._internal.park import ParkIdentity, build_park_identity
from tai42_agents.langchain_deep_agent.spec import InlineSkill
from tai42_agents.langchain_deep_agent.tool_spec import DeepSubAgentSpec


def build_rebuild_kwargs(
    *,
    tool_names: Sequence[str],
    subagents: list[DeepSubAgentSpec] | None,
    skills: list[str] | None,
    inline_skills: list[InlineSkill],
    rendered_system: str,
    interrupt_on: dict[str, Any] | None,
    response_format: Any,
    llm_provider: str | None,
    store_provider: str | None,
    llm_kwargs: dict[str, Any] | None,
    langgraph_config: dict[str, Any] | None,
    workspace_key: str,
) -> dict[str, Any]:
    """The JSON-serializable subset of a run's inputs that determines graph compilation.

    The identity a cross-worker resume recompiles the same graph from.
    Every value is a ``DeepAgentInput`` field name (subagents/inline_skills dumped to
    JSON, the system message the already-RENDERED text carried as a ``TemplatedText``
    inline ``content`` so resume never re-renders differently), so
    :meth:`DeepAgent.aresume_park` reconstructs the run inputs with
    ``ToolInput.model_validate``. The checkpoint provider and ``recursion_limit`` are
    pinned separately by
    :func:`~tai42_agents._internal.park.build_park_identity`, so they are deliberately
    absent here.

    ``workspace_key`` is the engine extra a cross-worker resume needs to REATTACH THE
    SAME durable volume; like ``recursion_limit`` it is NOT a ``DeepAgentInput`` field,
    so :meth:`DeepAgent.aresume_park` pops it out before the JSON inputs validate.
    """
    return {
        "tool_names": list(tool_names),
        "subagents": [spec.model_dump(mode="json") for spec in (subagents or [])],
        "skills": list(skills) if skills else None,
        "inline_skills": [skill.model_dump(mode="json") for skill in inline_skills],
        "system_message": {"content": rendered_system},
        "interrupt_on": interrupt_on,
        "response_format": response_format,
        "llm_provider": llm_provider,
        "store_provider": store_provider,
        "llm_kwargs": llm_kwargs,
        "langgraph_config": langgraph_config,
        "workspace_key": workspace_key,
    }


def build_astream_park(
    *,
    agent_name: str,
    config: dict[str, Any],
    tools: Sequence[StructuredTool],
    subagents: Sequence[NeutralSubAgentSpec | DeepSubAgentSpec] | None,
    tool_names: Sequence[str],
    skills: list[str] | None,
    inline_skills: list[InlineSkill],
    rendered_system: str,
    interrupt_on: dict[str, Any] | None,
    response_format: Any,
    llm_provider: str | None,
    store_provider: str | None,
    llm_kwargs: dict[str, Any] | None,
    langgraph_config: dict[str, Any] | None,
    checkpoint_provider: str | None,
    recursion_limit: int | None,
    workspace_key: str,
    workspace_retention_horizon: Any,
) -> ParkIdentity | None:
    """Assemble the streaming face's park identity, or ``None`` when the turn cannot park.

    The streaming face returns its stream to a caller that cannot receive a late
    answer, so it binds a resume path — and lets an async ask park — ONLY when a
    completion tool is bound in context (the conversation turn binds one to deliver
    the resumed answer). The completion tool is stored on the park entry and fired
    with the final answer on a clean terminal drive. A run carrying live tools or
    neutral (live) subagents is not rebuildable, so it never parks; with no completion
    bound, an async ask refuses loudly pre-persist. The retention bound is
    min(checkpoint, workspace). The binding's opaque context is stored beside the tool
    name and merged into the completion fire, so the delivery tool receives the address
    it routes by.
    """
    completion_tool, completion_context = get_park_completion()
    park_rebuildable = not tools and all(isinstance(s, DeepSubAgentSpec) for s in (subagents or []))
    if completion_tool is None or not park_rebuildable:
        return None
    return build_park_identity(
        agent_name=agent_name,
        config=config,
        checkpoint_provider=checkpoint_provider,
        has_live_tools=bool(tools),
        rebuild_kwargs=build_rebuild_kwargs(
            tool_names=tool_names,
            subagents=[s for s in (subagents or []) if isinstance(s, DeepSubAgentSpec)],
            skills=skills,
            inline_skills=inline_skills,
            rendered_system=rendered_system,
            interrupt_on=interrupt_on,
            response_format=response_format,
            llm_provider=llm_provider,
            store_provider=store_provider,
            llm_kwargs=llm_kwargs,
            langgraph_config=langgraph_config,
            workspace_key=workspace_key,
        ),
        recursion_limit=recursion_limit,
        completion_tool=completion_tool,
        completion_context=completion_context,
        bind=True,
        extra_retention_horizon=workspace_retention_horizon,
    )
