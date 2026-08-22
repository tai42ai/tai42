"""Build a compiled deep agent from resolved pieces.

Takes already-resolved infrastructure (LLM, store, checkpointer) and declarative
inputs (tools, subagents, skills, system prompt) and assembles a ``deepagents``
agent wired to the composite backend.

Context overflow is left to deepagents' built-in summarization; injecting the
project context-overflow stack here would double-compact.
"""

from __future__ import annotations

import asyncio
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware.subagents import (
    GENERAL_PURPOSE_SUBAGENT,
    CompiledSubAgent,
    SubAgent,
    SubAgentMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from tai42_contract.sandbox import SandboxSession
from tai42_kit.llm.middleware.leading_user import LeadingUserMiddleware
from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tai42_kit.llm.models import get_llm_async
from tai42_kit.llm.settings import llm_settings

from tai42_agents._internal.park import AsyncParkMiddleware
from tai42_agents._internal.recovery import _tool_error_middleware
from tai42_agents._internal.structured import as_tool_strategy
from tai42_agents.langchain_deep_agent.backend import SKILLS_ROOT, build_backend
from tai42_agents.langchain_deep_agent.sandbox_backend import build_sandbox_backend
from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec

# One shared, stateless park hook on every agent + subagent stack, so an async
# ``ask_user`` parked inside any of them (main, subagent, nested subagent, the
# auto-added general-purpose subagent) interrupts its own graph and resumes by id.
_async_park_middleware = AsyncParkMiddleware()

#: Tools deepagents injects into every agent. A user tool sharing one of these
#: names would shadow a built-in and make dispatch ambiguous.
_DEEPAGENTS_BUILTIN_TOOLS = frozenset(
    {"task", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "write_todos"}
)


def _general_purpose_subagent(skills: list[str] | None = None) -> SubAgent:
    """deepagents' auto-added general-purpose subagent, carrying the shared tool-error
    middleware on its own tool node.

    Supplying it explicitly (deepagents skips its auto-add when a subagent named
    ``general-purpose`` is present) is the only way to reach that stack — deepagents'
    own GP middleware-inheritance filter drops any middleware whose name is not a
    default GP slot. It reuses deepagents' name/description/prompt and sets no
    ``model``/``tools``, so the subagent still inherits the agent's model and tools and
    only tool-error visibility is added.

    ``skills`` are the same resolved skill sources this level passes to
    ``create_deep_agent``: deepagents has no ``inline_skills`` concept (inline skills
    are already flattened into ``skills`` sources), and its auto-added GP builds a
    ``SkillsMiddleware`` from that exact value — an explicit spec has no parent
    fallback, so the sources are forwarded here to match. ``_skills_with_inline``
    yields ``None`` or a non-empty list, so the key is set only when there are sources
    (matching the auto-add's ``if skills is not None`` / the inline path's truthiness).
    """
    sub: SubAgent = {**GENERAL_PURPOSE_SUBAGENT, "middleware": [_async_park_middleware, _tool_error_middleware]}
    if skills:
        sub["skills"] = skills
    return sub


def _validate(
    tools: list[StructuredTool],
    subagents: list[ResolvedSubAgentSpec],
    skills: list[str] | None,
) -> None:
    """Reject ambiguous configs before they reach deepagents.

    The harness dispatches tools and subagents by name and loads skills by path;
    duplicate names or off-root skill paths would fail silently inside the harness
    (ambiguous routing, or a skill loading nothing).
    """
    sub_names = [spec.name for spec in subagents]
    dup_subs = sorted({name for name in sub_names if sub_names.count(name) > 1})
    if dup_subs:
        raise ValueError(f"langchain_deep_agent has duplicate subagent names: {dup_subs}. Names must be unique.")

    for spec in subagents:
        if not spec.subagents:
            continue
        too_deep = sorted(child.name for child in spec.subagents if child.subagents)
        if too_deep:
            raise ValueError(
                f"subagent nesting is supported one level deep; nested subagents of "
                f"{spec.name!r} declare their own subagents: {too_deep}."
            )
        nested_names = [child.name for child in spec.subagents]
        dup_nested = sorted({name for name in nested_names if nested_names.count(name) > 1})
        if dup_nested:
            raise ValueError(
                f"subagent {spec.name!r} has duplicate nested subagent names: {dup_nested}. Names must be unique."
            )
        nested_builtin = sorted(set(nested_names) & _DEEPAGENTS_BUILTIN_TOOLS)
        if nested_builtin:
            raise ValueError(
                f"subagent {spec.name!r} nested subagent names collide with built-in tool "
                f"names: {nested_builtin}. Rename them."
            )

    # A subagent named like a built-in tool (e.g. "task") is ambiguous. ("general-
    # purpose" deliberately overrides deepagents' default subagent, so it is allowed.)
    sub_builtin = sorted(set(sub_names) & _DEEPAGENTS_BUILTIN_TOOLS)
    if sub_builtin:
        raise ValueError(
            f"langchain_deep_agent subagent names collide with built-in tool names: {sub_builtin}. Rename them."
        )

    tool_names = [tool.name for tool in tools]
    dup_tools = sorted({name for name in tool_names if tool_names.count(name) > 1})
    if dup_tools:
        raise ValueError(f"langchain_deep_agent has duplicate tool names: {dup_tools}. Names must be unique.")

    clobbered = sorted(set(tool_names) & _DEEPAGENTS_BUILTIN_TOOLS)
    if clobbered:
        raise ValueError(
            f"langchain_deep_agent tools collide with deepagents built-in tools: {clobbered}. Rename them."
        )

    # Only the reference ``skills`` list is path-checked (source paths rooted under
    # SKILLS_ROOT). Inline skills are names, validated separately.
    all_skills = list(skills or [])
    for spec in subagents:
        all_skills.extend(spec.skills or [])
        for child in spec.subagents:
            all_skills.extend(child.skills or [])
    off_root = sorted(path for path in all_skills if not path.startswith(SKILLS_ROOT))
    if off_root:
        raise ValueError(
            f"langchain_deep_agent skill paths must start with {SKILLS_ROOT!r}: {off_root}. "
            f"Off-root paths route to the scratch backend and load no skills."
        )


def _inline_skill_source(name: str) -> str:
    """Skills-middleware source path for an inline skill ``name`` (``/skills/<name>/``)."""
    return f"{SKILLS_ROOT}{name}/"


def _merge_inline_skills(target: dict[str, str], inline_skills: list[InlineSkill] | None) -> None:
    """Merge ``inline_skills`` into ``target`` (name -> content), raising on conflicts.

    Two inline skills may share a name only if they carry identical content;
    differing content for the same name is an ambiguous configuration and raises
    loudly rather than silently picking a winner.
    """
    for skill in inline_skills or []:
        existing = target.get(skill.name)
        if existing is not None and existing != skill.content:
            raise ValueError(
                f"langchain_deep_agent inline skill {skill.name!r} supplied twice with different content. "
                f"Inline skill names must be unique unless their content is identical."
            )
        target[skill.name] = skill.content


def _collect_inline_skills(
    main_inline_skills: list[InlineSkill] | None,
    subagents: list[ResolvedSubAgentSpec],
) -> dict[str, str]:
    """Collect inline skills from the main agent + every subagent + nested into one map.

    Returns a single ``name -> SKILL.md content`` dict spanning all agents. A name
    reused across agents with differing content raises (see
    :func:`_merge_inline_skills`); identical content is shared (one mount serves
    every agent that declares it).
    """
    collected: dict[str, str] = {}
    _merge_inline_skills(collected, main_inline_skills)
    for spec in subagents:
        _merge_inline_skills(collected, spec.inline_skills)
        for child in spec.subagents:
            _merge_inline_skills(collected, child.inline_skills)
    return collected


def _skills_with_inline(
    skills: list[str] | None,
    inline_skills: list[InlineSkill] | None,
) -> list[str] | None:
    """Extend an agent's loaded ``skills`` sources with its own inline skills.

    Each inline skill's ``/skills/<name>/`` source is appended (preserving order,
    skipping any already listed) so supplying an inline skill loads it without the
    caller also naming it in ``skills``. Returns ``None`` when the agent has
    neither reference nor inline skills, so deepagents adds no skills middleware.
    """
    sources = list(skills or [])
    for skill in inline_skills or []:
        source = _inline_skill_source(skill.name)
        if source not in sources:
            sources.append(source)
    return sources or None


async def _resolve_subagent(
    spec: ResolvedSubAgentSpec,
    *,
    llm: BaseChatModel | None = None,
    tools: list[StructuredTool] | None = None,
    store: BaseStore | None = None,
    backend: Any | None = None,
) -> SubAgent:
    """Convert a :class:`ResolvedSubAgentSpec` into a deepagents ``SubAgent`` dict.

    Only the keys the caller set are emitted, so deepagents applies its own
    inheritance for the rest (parent model, tools, ``interrupt_on``).

    ``llm`` / ``tools`` / ``store`` / ``backend`` (the main agent's resolved pieces)
    are required only when ``spec.subagents`` is non-empty: nested subagents are
    compiled into their own deep agents and attached through a
    :class:`SubAgentMiddleware`, since deepagents' inheritance covers one level only.
    """
    sub: SubAgent = {
        "name": spec.name,
        "description": spec.description,
        "system_prompt": spec.system_prompt,
    }
    if spec.tools:
        sub["tools"] = list(spec.tools)
    skills = _skills_with_inline(spec.skills, spec.inline_skills)
    if skills:
        sub["skills"] = skills
    if spec.interrupt_on:
        sub["interrupt_on"] = spec.interrupt_on
    if spec.response_format is not None:
        sub["response_format"] = as_tool_strategy(spec.response_format)
    if spec.llm_provider:
        sub["model"] = await get_llm_async(
            provider=spec.llm_provider,
            **llm_settings().with_fallbacks(spec.llm_kwargs or {}),
        )
    # Every subagent stack carries the park hook (async ask parks the subagent's own
    # graph) and merges the shared tool-error middleware onto its own tool node
    # (deepagents forwards spec ``middleware`` to the subagent's create_agent), so a
    # tool-logic failure inside a subagent surfaces to its model instead of aborting.
    sub_middleware: list[Any] = [_async_park_middleware]
    if spec.subagents:
        parent_model = sub.get("model") or llm
        if parent_model is None or store is None or backend is None:
            raise ValueError(
                f"subagent {spec.name!r} declares nested subagents — resolving it requires "
                f"the main agent's llm, store and backend."
            )
        parent_tools = list(spec.tools) or list(tools or [])
        nested = await asyncio.gather(
            *(
                _compile_nested_subagent(
                    child,
                    parent_model=parent_model,
                    parent_tools=parent_tools,
                    store=store,
                    backend=backend,
                )
                for child in spec.subagents
            )
        )
        sub_middleware.append(SubAgentMiddleware(backend=backend, subagents=list(nested)))
    sub_middleware.append(_tool_error_middleware)
    sub["middleware"] = sub_middleware
    return sub


async def _compile_nested_subagent(
    child: ResolvedSubAgentSpec,
    *,
    parent_model: Any,
    parent_tools: list[StructuredTool],
    store: BaseStore,
    backend: Any,
) -> CompiledSubAgent:
    """Compile a nested subagent into a deepagents ``CompiledSubAgent``.

    The child is built as its own deep agent (full default middleware stack plus its
    skills) and handed to the parent's ``SubAgentMiddleware`` as a runnable. Model
    and tools inherit the parent's unless the child sets its own.
    """
    model = parent_model
    if child.llm_provider:
        model = await get_llm_async(
            provider=child.llm_provider,
            **llm_settings().with_fallbacks(child.llm_kwargs or {}),
        )
    child_skills = _skills_with_inline(child.skills, child.inline_skills)
    runnable = create_deep_agent(
        model=model,
        tools=list(child.tools) or parent_tools,
        system_prompt=child.system_prompt,
        skills=child_skills,
        backend=backend,
        store=store,
        interrupt_on=child.interrupt_on,
        response_format=as_tool_strategy(child.response_format),
        # Same park hook + tool-error visibility on the nested subagent's own tool node
        # and on its own auto-added general-purpose subagent, which inherits the child's
        # skills.
        middleware=[_async_park_middleware, _tool_error_middleware],
        subagents=[_general_purpose_subagent(child_skills)],
    )
    return {"name": child.name, "description": child.description, "runnable": runnable}


async def build_langchain_deep_agent(
    *,
    llm: BaseChatModel,
    store: BaseStore,
    checkpointer: BaseCheckpointSaver,
    tools: list[StructuredTool] | None = None,
    subagents: list[ResolvedSubAgentSpec] | None = None,
    skills: list[str] | None = None,
    inline_skills: list[InlineSkill] | None = None,
    system_prompt: str | None = None,
    interrupt_on: dict[str, Any] | None = None,
    response_format: Any | None = None,
    session: SandboxSession | None = None,
) -> CompiledStateGraph:
    """Assemble a compiled deep agent over the composite backend.

    ``llm``, ``store`` and ``checkpointer`` are the resolved registry resources.
    ``subagents`` are declarative specs resolved to deepagents ``SubAgent`` dicts; a
    spec may carry one level of nested ``subagents``. ``skills`` are source paths
    under :data:`tai42_agents.langchain_deep_agent.backend.SKILLS_ROOT`.

    ``inline_skills`` (each a name + ``SKILL.md`` body) are collected from the main
    agent and every subagent into one mount, and each agent's own inline skills are
    auto-loaded as ``SKILLS_ROOT<name>/`` sources.

    ``response_format`` (a pydantic model or langchain response strategy) makes the
    agent return a validated structured object in ``state['structured_response']``;
    a raw schema is routed through the tool-calling strategy so structured output
    never depends on provider-native support. ``None`` keeps free-form text.

    ``session`` is the acquired durable sandbox session for a run/astream drive: when set the
    scratch backend is a :class:`~tai42_agents.langchain_deep_agent.sandbox_backend.SandboxSessionBackend`
    over the workspace VOLUME (§B2); when ``None`` (the append path) it stays the non-sandbox
    ``StateBackend`` — the hard sandbox dependency lives at the run/astream door, not here.
    """
    subagent_specs = list(subagents or [])
    _validate(tools or [], subagent_specs, skills)

    inline_skill_contents = _collect_inline_skills(inline_skills, subagent_specs)
    backend = (
        build_sandbox_backend(session, inline_skill_contents or None)
        if session is not None
        else build_backend(inline_skill_contents or None)
    )
    resolved = await asyncio.gather(
        *(_resolve_subagent(spec, llm=llm, tools=tools or [], store=store, backend=backend) for spec in subagent_specs)
    )
    main_skills = _skills_with_inline(skills, inline_skills)
    resolved_subagents: list[SubAgent | CompiledSubAgent] = list(resolved)
    # deepagents auto-adds a general-purpose subagent when the caller supplies none;
    # supply it explicitly (with the same skills it would have inherited) so its tool
    # node carries the shared middleware too (a caller who names their own
    # general-purpose subagent already gets it via _resolve_subagent).
    if not any(sub.get("name") == GENERAL_PURPOSE_SUBAGENT["name"] for sub in resolved_subagents):
        resolved_subagents.insert(0, _general_purpose_subagent(main_skills))

    return create_deep_agent(
        model=llm,
        tools=tools or [],
        system_prompt=system_prompt,
        subagents=resolved_subagents,
        skills=main_skills,
        backend=backend,
        checkpointer=checkpointer,
        store=store,
        interrupt_on=interrupt_on,
        response_format=as_tool_strategy(response_format),
        # The park hook leads: it is the loop's sole before_model hook, so it is the
        # first per-step hook and recognizes an async-ask park before any compacting
        # hook (all of which compact through wrap_model_call, skipped on a park
        # super-step) could evict its marked ToolMessage. The system purge leads the
        # rest so a stored system message never reaches the model alongside the per-run
        # prompt (state never carries one). The leading-user middleware keeps a thread
        # that opens with an assistant message user-first for strict-ordering providers.
        # The rolling-cache-mark middleware keeps a per-turn-marked thread to one cache
        # breakpoint at the call. A tool-logic failure surfaces to the model as an error
        # ToolMessage rather than aborting the run; every other exception stays a loud abort.
        middleware=[
            _async_park_middleware,
            SystemPurgeMiddleware(),
            LeadingUserMiddleware(),
            RollingCacheMarkMiddleware(),
            _tool_error_middleware,
        ],
    )
