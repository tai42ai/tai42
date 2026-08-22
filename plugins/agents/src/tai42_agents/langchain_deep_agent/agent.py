"""``langchain_deep_agent`` as an :class:`Agent`.

Two faces built from the one shared streaming core (:meth:`DeepAgent._astream_built`):

* :meth:`DeepAgent.run` — the JSON tool-face. Renders the messages, resolves tool
  names + JSON subagents into live tools + core specs, then drains the streaming
  path via :meth:`Agent._drain`, returning the structured object or answer string.
  A run that pauses on an interrupt raises
  :class:`~tai42_contract.agent.base.AgentInterruptedError`.
* :meth:`DeepAgent.astream` — the in-process streaming face driven with live tool
  closures; surfaces each pending platform interrupt as an :class:`InterruptFinal`
  after the stream drains.

A requested ``response_format`` is honored on both faces: the invoke face returns
the structured value and the streaming face emits :class:`StructuredFinal`; a run
that produces none raises rather than silently omitting it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tai42_contract.agent import Agent
from tai42_contract.agent.base import PresetSpec
from tai42_contract.agent.base import SubAgentSpec as NeutralSubAgentSpec
from tai42_contract.agent.events import InterruptFinal, StreamEvent, StructuredFinal, SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.interactions import get_park_completion_tool
from tai42_contract.sandbox import SandboxSession
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.models import get_llm_async
from tai42_kit.llm.runtime import build_agent_input, build_user_output, extract_structured_output
from tai42_kit.llm.settings import llm_provider_settings, llm_settings
from tai42_kit.llm.store.store_registry import store_registry

from tai42_agents._internal.append import awrite_thread_messages, require_thread_id, to_thread_messages
from tai42_agents._internal.config_util import build_run_config, init_langgraph_config
from tai42_agents._internal.park import (
    ParkIdentity,
    build_park_identity,
    finalize_drive,
    park_continuation,
    register_agent_resume_tool,
)
from tai42_agents._internal.park.driver import _collect_pending_interrupts, _is_suspended_receipt
from tai42_agents._internal.park.errors import AgentResumeInterruptNotPendingError
from tai42_agents._internal.recovery import _repair_dangling_tool_calls
from tai42_agents._internal.reject import (
    reject_blank_memory_keys,
    reject_unhonored,
    reject_untitled_response_format,
)
from tai42_agents._internal.render import render_message
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import as_tool_strategy
from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent
from tai42_agents.langchain_deep_agent.session import DeepAgentSession
from tai42_agents.langchain_deep_agent.settings import langchain_deep_agent_crash_resume
from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec
from tai42_agents.langchain_deep_agent.tool_spec import DeepSubAgentSpec, resolve_subagent_specs

# The two ABC ``run``/``astream`` parameters ``langchain_deep_agent`` cannot honor on the
# main agent, mapped to the reason named in the raised error (the keys define this
# agent's unhonored set). ``presets`` is truthiness-checked, ``strategy`` set when
# not ``None``.
_UNHONORED_REASONS: dict[str, str] = {
    "presets": (
        "its tool set is composed from tool_names and live tools on the main agent, not presets, "
        "and it will not silently ignore one"
    ),
    "strategy": "the deepagents runtime applies no composition strategy and will not silently ignore one",
    "system_content_kwargs": (
        "its system prompt is handed to the deepagents factory, never built as a content block through "
        "build_system_message, so it cannot carry content-block keys; use user_content_kwargs instead"
    ),
    "resume_checkpoint_id": (
        "the durable sandbox WORKSPACE volume cannot be forked alongside the LangGraph checkpoint, so "
        "forking the checkpoint past an aborted turn would run a forked graph over post-abort workspace "
        "state — a silent divergence; it is unhonored on the durable deep agent (user ruling 2026-08-21)"
    ),
}
_UNHONORED_COLLECTION_PARAMS: frozenset[str] = frozenset({"presets"})


class DeepAgentInput(BaseModel):
    """JSON tool-face parameters for ``langchain_deep_agent``. Live ``tools=`` are absent
    from this JSON schema (a live ``StructuredTool`` is not JSON-serializable), but
    both in-process faces — :meth:`DeepAgent.run` and :meth:`DeepAgent.astream` —
    accept them directly.

    The schema advertises exactly the composable fields ``langchain_deep_agent``'s runtime
    honors — ``subagents``, ``skills``, ``inline_skills``, ``interrupt_on``,
    ``response_format`` alongside the ``tool_names`` / message / provider plumbing.
    It carries no ``strategy`` field: the deepagents runtime has no composition
    strategy to apply (its sub-agent path rejects a per-sub ``strategy`` outright),
    so advertising one would be a schema lie. ``extra="forbid"`` rejects any
    unknown key loudly at validation rather than letting a typo at the run door
    vanish silently.

    ``base_url``/``api_key`` in ``llm_kwargs`` legitimately route to a caller-chosen
    model endpoint; expose any agent or tool carrying these kwargs only to trusted
    callers — an injected parent agent could redirect the model call to a hostile
    endpoint and leak the key/context.
    """

    model_config = ConfigDict(extra="forbid")

    tool_names: list[str] = Field(default_factory=list, description="Client tool names to load.")
    subagents: list[DeepSubAgentSpec] | None = Field(
        default=None, description="Subagents the main agent can invoke via its task tool."
    )
    skills: list[str] | None = Field(default=None, description="Skill source paths under SKILLS_ROOT.")
    inline_skills: list[InlineSkill] | None = Field(
        default=None, description="Skills supplied inline (name + SKILL.md content)."
    )
    system_message: str | None = ""
    user_message: str | None = ""
    system_message_id: str | None = ""
    user_message_id: str | None = ""
    system_message_kwargs: dict[str, Any] | None = None
    user_message_kwargs: dict[str, Any] | None = None
    interrupt_on: dict[str, Any] | None = None
    response_format: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the forced structured output (needs a top-level 'title')."
    )
    user_content_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-block keys merged into the user message's text block (e.g. cache_control "
            "for Anthropic prompt caching). Provider-unknown keys surface as loud provider errors. "
            "On a checkpointed thread the model call keeps only the newest mark (older marks are "
            "stripped), so per-turn marking stays within the provider's breakpoint cap (Anthropic: 4)."
        ),
    )
    llm_provider: str | None = None
    checkpoint_provider: str | None = None
    store_provider: str | None = None
    llm_kwargs: dict[str, Any] | None = None
    langgraph_config: dict[str, Any] | None = None

    @field_validator("user_content_kwargs")
    @classmethod
    def _empty_content_kwargs_is_unset(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """An empty dict carries no content-block keys — normalize {} to None so it
        reads as unset, matching the builders that treat {} as no mark."""
        return value or None


async def _to_internal(spec: NeutralSubAgentSpec | DeepSubAgentSpec) -> ResolvedSubAgentSpec:
    """Resolve either subagent shape into the core spec.

    A programmatic caller passes :class:`NeutralSubAgentSpec` (live tools), the JSON
    door :class:`DeepSubAgentSpec` (tool names); both faces accept both.
    """
    if isinstance(spec, DeepSubAgentSpec):
        return (await resolve_subagent_specs([spec]))[0]
    return await _neutral_to_internal(spec)


async def _neutral_to_internal(spec: NeutralSubAgentSpec) -> ResolvedSubAgentSpec:
    """Map a neutral (live-tools) sub-agent spec to the internal deepagents spec.

    Resolves the spec's ``tools`` / ``tool_names`` / ``presets`` into a flat
    ``StructuredTool`` list. The internal spec has no ``strategy`` field, so a
    neutral ``strategy`` is rejected rather than dropped silently.
    """
    if spec.strategy is not None:
        raise ValueError(
            f"sub-agent {spec.name!r} sets strategy={spec.strategy!r}, which the "
            "deepagents sub-agent spec cannot carry; pass response_format as a "
            "ToolStrategy on the parent instead."
        )
    tools = await resolve_tools(tai42_app.tools, list(spec.tool_names), list(spec.tools), list(spec.presets))
    subagents = [await _neutral_to_internal(child) for child in spec.subagents]
    # Neutral inline_skills are plain dicts; coerce to InlineSkill for the factory.
    reject_untitled_response_format(f"subagent {spec.name!r}", spec.response_format)
    inline_skills = [s if isinstance(s, InlineSkill) else InlineSkill(**s) for s in (spec.inline_skills or [])]
    return ResolvedSubAgentSpec(
        name=spec.name,
        description=spec.description,
        system_prompt=spec.system_prompt,
        tools=tools,
        skills=list(spec.skills) or None,
        inline_skills=inline_skills or None,
        response_format=spec.response_format,
        subagents=subagents,
    )


# A parking agent binds the hidden ``agent_resume`` continuation from its OWN registration:
# the park package no longer binds it as a module-import side effect. Per-epoch idempotent, so
# a box loading several parking agents binds it exactly once.
register_agent_resume_tool()


# The §B4 ``crash_resume`` setting is DECLARED to the skeleton at registration as
# ``meta={"tai42/crash_resume": <setting>}`` on the run tool, threaded through the generic
# ``agents.agent(name, tags=..., meta=...)`` passthrough; the run-dispatch seam reads that key to
# decide whether to re-invoke a recycled detached run. Captured ONCE at registration (the setting
# is recycle-class, so a hot change re-registers and re-declares it). Sourced from the lightweight
# ``langchain_deep_agent_crash_resume`` read — reading the full settings here would couple this
# module's IMPORT to the REQUIRED digest-pinned ``session_image``, regressing a zero-config import;
# full validation still fires at the first run/astream drive.
@tai42_app.agents.agent(
    "langchain_deep_agent",
    tags={"agents"},
    meta={"tai42/crash_resume": langchain_deep_agent_crash_resume()},
)
class DeepAgent(Agent):
    tool_name: ClassVar[str] = "langchain_deep_agent"
    tool_description: ClassVar[str] = (
        "Create and run a deep agent (planning + subagents + skills + filesystem). "
        "Loads tools by name and runs with the given system/user messages. With "
        "response_format set, returns a validated structured object and fails loudly "
        "if the agent produces none."
    )
    ToolInput: ClassVar[type[BaseModel]] = DeepAgentInput

    async def run(
        self,
        *,
        tools: Sequence[StructuredTool] = (),
        tool_names: Sequence[str] = (),
        presets: Sequence[PresetSpec] | None = None,
        subagents: list[DeepSubAgentSpec] | None = None,
        skills: list[str] | None = None,
        inline_skills: list[InlineSkill] | None = None,
        system_message: str = "",
        user_message: str = "",
        system_message_id: str = "",
        user_message_id: str = "",
        system_message_kwargs: dict[str, Any] | None = None,
        user_message_kwargs: dict[str, Any] | None = None,
        interrupt_on: dict[str, Any] | None = None,
        response_format: Any = None,
        strategy: str | None = None,
        system_content_kwargs: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        resume_checkpoint_id: str | None = None,
        recursion_limit: int | None = None,
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        store_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        """Resolve the JSON tool inputs and drain the streaming core to a value.

        Live ``tools`` combine with the client tools from ``tool_names``, the JSON
        subagents resolve to core specs, the messages render, and the streaming core
        is drained via :meth:`Agent._drain`: the structured object
        (``response_format`` set) or the answer string is returned, and a run pausing
        on an interrupt raises
        :class:`~tai42_contract.agent.base.AgentInterruptedError`.

        Provide exactly one of ``user_message`` (a fresh turn) or ``resume``
        (answering a prior interrupt); ``resume_checkpoint_id`` forks past an aborted
        turn. A ``response_format`` must be a JSON Schema with a top-level ``title``.

        ``user_content_kwargs`` merges content-block keys (e.g. ``cache_control``)
        onto the user message's text block; a provider-unknown key surfaces as a loud
        provider error. ``presets``, ``strategy`` and ``system_content_kwargs`` are
        not honored here; each raises (the system prompt never becomes a content
        block through ``build_system_message``).
        """
        reject_unhonored(
            "langchain_deep_agent.run",
            {
                "presets": presets,
                "strategy": strategy,
                "system_content_kwargs": system_content_kwargs,
                "resume_checkpoint_id": resume_checkpoint_id,
            },
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys("langchain_deep_agent.run", thread_id=thread_id, resume_checkpoint_id=None)
        reject_untitled_response_format("langchain_deep_agent", response_format)

        if resume is None and not (user_message or user_message_id):
            raise ValueError("langchain_deep_agent.run requires exactly one of user_message or resume")
        if resume is not None:
            if user_message or user_message_id:
                raise ValueError("langchain_deep_agent.run requires exactly one of user_message or resume, not both.")
            if user_content_kwargs:
                raise ValueError(
                    "langchain_deep_agent.run: user_content_kwargs applies to a fresh user_message turn, not a resume"
                )
            rendered_user: str | None = None
        else:
            rendered_user = await render_message(user_message, user_message_id, user_message_kwargs, allow_empty=False)
        rendered_system = await render_message(system_message, system_message_id, system_message_kwargs)

        client_tools = await tai42_app.tools.get_client_tools(list(tool_names)) if tool_names else []
        resolved_tools = [*tools, *client_tools]
        internal_subagents = await resolve_subagent_specs(subagents)
        coerced_inline_skills = [s if isinstance(s, InlineSkill) else InlineSkill(**s) for s in (inline_skills or [])]

        # Wrap the schema ONCE and bind that same strategy into both the graph and the
        # projection, so the synthetic tool names match by identity.
        strategy = as_tool_strategy(response_format)

        # HARD sandbox dependency (§B3.7): the scratch backend is durable, so a run requires a
        # provider — acquire the session BEFORE the graph compiles (the backend needs it), a
        # loud SandboxUnavailableError on a box with none. A threaded run reattaches its durable
        # volume by workspace_key; a tool-face run gets a fresh ephemeral one. The workspace lease
        # wraps session-acquire + cred-materialize + drive as one guarded region (§B3.2), so two
        # same-thread_id workers never each open a session on the shared volume (the lease-loser
        # raises before any session/cred exists).
        async with DeepAgentSession.leased(thread_id=thread_id) as drive:
            suspended = False
            try:
                agent = await self._resolve_and_build(
                    tools=resolved_tools,
                    subagents=internal_subagents,
                    skills=skills,
                    inline_skills=coerced_inline_skills or None,
                    system_message=rendered_system,
                    response_format=strategy,
                    interrupt_on=interrupt_on,
                    llm_provider=llm_provider,
                    checkpoint_provider=checkpoint_provider,
                    store_provider=store_provider,
                    llm_kwargs=llm_kwargs,
                    session=drive.session,
                )
                config = self._run_config(langgraph_config, thread_id, None, recursion_limit)
                if resume is not None:
                    agent_input: Any = Command(resume=resume)
                else:
                    # The resume/user guard above makes rendered_user a str on this branch.
                    assert rendered_user is not None
                    agent_input = build_agent_input(rendered_user, user_content_kwargs=user_content_kwargs)

                # The run face returns the park RECEIPT to its caller and resumes out of band, so
                # it binds the resume continuation: an async ask_user this run drives parks through
                # it. No completion tool is bound here, so a resumed run's final text is delivered
                # nowhere — its side effects are the product; a caller needing the answer must
                # invoke through a completion-bound door. A run carrying live tools, a non-durable
                # checkpoint, an ephemeral (tool-face) workspace, or an unconfigured park index is
                # not park-capable — build_park_identity returns None and the async ask refuses
                # loudly pre-persist. The park's retention bound is min(checkpoint, workspace): the
                # paused graph lives only as long as BOTH stores hold it (§B3.1).
                park = build_park_identity(
                    agent_name=self.tool_name,
                    config=config,
                    checkpoint_provider=checkpoint_provider,
                    has_live_tools=bool(tools),
                    rebuild_kwargs=self._rebuild_kwargs(
                        tool_names=tool_names,
                        subagents=subagents,
                        skills=skills,
                        inline_skills=coerced_inline_skills,
                        rendered_system=rendered_system,
                        interrupt_on=interrupt_on,
                        response_format=response_format,
                        llm_provider=llm_provider,
                        store_provider=store_provider,
                        llm_kwargs=llm_kwargs,
                        langgraph_config=langgraph_config,
                        workspace_key=drive.workspace_key,
                    ),
                    recursion_limit=recursion_limit,
                    bind=True,
                    extra_retention_horizon=drive.workspace_retention_horizon,
                )
                with park_continuation(park):
                    result = await self._drain(
                        self._astream_built(
                            agent,
                            agent_input,
                            config,
                            interrupt_on,
                            response_format=response_format,
                            structured_strategy=strategy,
                            park=park,
                        ),
                        response_format=response_format,
                    )
                # A park-suspend is still a LIVE run: skip the credential scrub so the bearer file
                # stays for the door-less expiry resume (its own terminal exit scrubs it, §B4).
                suspended = _is_suspended_receipt(result)
                return result
            finally:
                if not suspended:
                    await drive.scrub_credentials()

    async def append_thread_messages(
        self,
        *,
        thread_id: str,
        messages: list[dict[str, str]],
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        store_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        """Append ``messages`` to the thread's stored history without running the model.

        ``messages`` items are ``{"role": "user"|"assistant", "content": str}`` — an
        unknown role or blank content raises. ``thread_id`` (or a
        ``configurable.thread_id`` carried in ``langgraph_config``) names the thread to
        append to; a missing one raises rather than minting a fresh thread. The deep
        agent compiles the SAME way a run does (over the resolved checkpointer and
        store) but with no tools or sub-agents — the checkpoint write is
        tool-independent — and no model call is made.

        This path acquires NO sandbox session (``session=None`` → the non-sandbox
        ``StateBackend`` default): it does no file work and makes no model call, so a
        deployment can record manual-mode history without a live sandbox — the sandbox hard
        dependency is on the run/astream drive only (§B3.5), never here.
        """
        converted = to_thread_messages(messages)
        reject_blank_memory_keys(
            "langchain_deep_agent.append_thread_messages", thread_id=thread_id, resume_checkpoint_id=None
        )
        config = build_run_config(langgraph_config, thread_id)
        require_thread_id("langchain_deep_agent.append_thread_messages", config)
        agent = await self._resolve_and_build(
            tools=[],
            subagents=[],
            skills=None,
            inline_skills=None,
            system_message="",
            response_format=None,
            interrupt_on=None,
            llm_provider=llm_provider,
            checkpoint_provider=checkpoint_provider,
            store_provider=store_provider,
            llm_kwargs=llm_kwargs,
            # No sandbox session — the non-sandbox StateBackend default; a checkpoint-only write
            # does no file work and requires no live sandbox (§B3.5).
            session=None,
        )
        await awrite_thread_messages(agent, config, converted)

    @staticmethod
    def _run_config(
        langgraph_config: dict[str, Any] | None,
        thread_id: str | None,
        resume_checkpoint_id: str | None,
        recursion_limit: int | None,
    ) -> dict[str, Any]:
        """Build the run config both faces run the graph with.

        The caller's ``langgraph_config`` is the read-only base; ``thread_id`` /
        ``resume_checkpoint_id`` overlay its ``configurable`` and ``recursion_limit``
        overlays the top level, through
        :func:`~tai42_agents._internal.config_util.build_run_config`. With no thread
        pinned, :func:`init_langgraph_config` mints a fresh isolated one.

        ``recursion_limit`` bounds the TOP-LEVEL graph ONLY: each task-tool subagent
        runs its own graph bound by deepagents at 9999, so the effective step budget
        is MULTIPLICATIVE across nesting depth, not a total-spend ceiling. The
        settings default bounds only the top level.
        """
        return init_langgraph_config(
            config=build_run_config(langgraph_config, thread_id, resume_checkpoint_id, recursion_limit)
        )

    async def astream(
        self,
        *,
        tools: Sequence[StructuredTool] = (),
        tool_names: Sequence[str] = (),
        presets: Sequence[PresetSpec] | None = None,
        subagents: Sequence[NeutralSubAgentSpec | DeepSubAgentSpec] | None = None,
        skills: list[str] | None = None,
        inline_skills: Sequence[dict[str, Any]] | None = None,
        system_message: str = "",
        user_message: str | None = None,
        response_format: Any = None,
        strategy: str | None = None,
        system_content_kwargs: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        interrupt_on: dict[str, Any] | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        resume_checkpoint_id: str | None = None,
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        store_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        recursion_limit: int | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Run one turn and yield the platform event stream, then one
        :class:`InterruptFinal` per pending interrupt.

        Provide exactly one of ``user_message`` (a fresh turn) or ``resume``
        (answering a prior interrupt). The system prompt is passed verbatim (the API
        resolved it already). Live ``tools`` combine with the client tools from
        ``tool_names``.

        ``langgraph_config`` is the base run config (built through :meth:`_run_config`
        as :meth:`run`): a ``configurable.thread_id`` / ``checkpoint_id`` it carries
        pins checkpointed memory, and ``thread_id`` / ``resume_checkpoint_id`` /
        ``recursion_limit`` overlay it.

        A requested ``response_format`` that produces no structured result raises
        after the stream drains rather than silently omitting it. ``user_content_kwargs``
        merges content-block keys (e.g. ``cache_control``) onto the user message's text
        block; a provider-unknown key surfaces as a loud provider error. ``presets``,
        ``strategy`` and ``system_content_kwargs`` are not honored here; each raises.
        """
        reject_unhonored(
            "langchain_deep_agent.astream",
            {
                "presets": presets,
                "strategy": strategy,
                "system_content_kwargs": system_content_kwargs,
                "resume_checkpoint_id": resume_checkpoint_id,
            },
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys("langchain_deep_agent.astream", thread_id=thread_id, resume_checkpoint_id=None)
        reject_untitled_response_format("langchain_deep_agent", response_format)
        if (user_message is None) == (resume is None):
            raise ValueError("langchain_deep_agent.astream requires exactly one of user_message or resume")
        if resume is not None and user_content_kwargs:
            raise ValueError(
                "langchain_deep_agent.astream: user_content_kwargs applies to a fresh user_message turn, not a resume"
            )

        client_tools = await tai42_app.tools.get_client_tools(list(tool_names)) if tool_names else []
        internal_subagents = [await _to_internal(spec) for spec in (subagents or [])]
        coerced_inline_skills = [s if isinstance(s, InlineSkill) else InlineSkill(**s) for s in (inline_skills or [])]
        # Wrap the schema ONCE and bind that same strategy into both the graph and the
        # projection, so the synthetic tool names match by identity.
        strategy = as_tool_strategy(response_format)

        # HARD sandbox dependency (§B3.7): acquire the durable session BEFORE compile and thread
        # it into the backend, a loud SandboxUnavailableError on a box with none. The workspace
        # lease wraps session-acquire + cred-materialize + drive as one guarded region so threaded
        # turns serialize across workers (§B3.2) and the lease-loser never opens a leaked session; a
        # tool-face run takes none.
        async with DeepAgentSession.leased(thread_id=thread_id) as drive:
            saw_structured = False
            saw_interrupt = False
            saw_suspended = False
            try:
                agent, config = await self._build_agent(
                    tools=[*tools, *client_tools],
                    subagents=internal_subagents,
                    skills=skills,
                    inline_skills=coerced_inline_skills or None,
                    system_message=system_message,
                    response_format=strategy,
                    interrupt_on=interrupt_on,
                    thread_id=thread_id,
                    resume_checkpoint_id=None,
                    llm_provider=llm_provider,
                    checkpoint_provider=checkpoint_provider,
                    store_provider=store_provider,
                    llm_kwargs=llm_kwargs,
                    recursion_limit=recursion_limit,
                    langgraph_config=langgraph_config,
                    session=drive.session,
                )
                if resume is not None:
                    agent_input: Any = Command(resume=resume)
                else:
                    # The exactly-one-of guard above makes user_message non-None here.
                    assert user_message is not None
                    agent_input = build_agent_input(user_message, user_content_kwargs=user_content_kwargs)

                # The streaming face returns its stream to a caller that cannot receive a late
                # answer, so it binds a resume path — and lets an async ask park — ONLY when a
                # completion tool is bound in context (the conversation turn binds one to deliver
                # the resumed answer). The completion tool is stored on the park entry and fired
                # with the final answer on a clean terminal drive. A run carrying live tools or
                # neutral (live) subagents is not rebuildable, so it never parks; with no
                # completion bound, an async ask refuses loudly pre-persist. The retention bound is
                # min(checkpoint, workspace) (§B3.1).
                completion_tool = get_park_completion_tool()
                park: ParkIdentity | None = None
                park_rebuildable = not tools and all(isinstance(s, DeepSubAgentSpec) for s in (subagents or []))
                if completion_tool is not None and park_rebuildable:
                    park = build_park_identity(
                        agent_name=self.tool_name,
                        config=config,
                        checkpoint_provider=checkpoint_provider,
                        has_live_tools=bool(tools),
                        rebuild_kwargs=self._rebuild_kwargs(
                            tool_names=tool_names,
                            subagents=[s for s in (subagents or []) if isinstance(s, DeepSubAgentSpec)],
                            skills=skills,
                            inline_skills=coerced_inline_skills,
                            rendered_system=system_message,
                            interrupt_on=interrupt_on,
                            response_format=response_format,
                            llm_provider=llm_provider,
                            store_provider=store_provider,
                            llm_kwargs=llm_kwargs,
                            langgraph_config=langgraph_config,
                            workspace_key=drive.workspace_key,
                        ),
                        recursion_limit=recursion_limit,
                        completion_tool=completion_tool,
                        bind=True,
                        extra_retention_horizon=drive.workspace_retention_horizon,
                    )

                with park_continuation(park):
                    async for event in self._astream_built(
                        agent,
                        agent_input,
                        config,
                        interrupt_on,
                        response_format=response_format,
                        structured_strategy=strategy,
                        park=park,
                    ):
                        if isinstance(event, StructuredFinal):
                            saw_structured = True
                        elif isinstance(event, InterruptFinal):
                            saw_interrupt = True
                        elif isinstance(event, SuspendedFinal):
                            saw_suspended = True
                        yield event
                # A requested response_format that produced no StructuredFinal fails loudly.
                # A pending interrupt OR an async park means the run paused rather than finished,
                # so (as in _drain) the pause takes precedence and the raise is skipped.
                if response_format is not None and not saw_structured and not saw_interrupt and not saw_suspended:
                    raise RuntimeError("agent run requested a response_format but produced no structured output")
            finally:
                # A park-suspend is still LIVE: keep the bearer file for the door-less expiry
                # resume (its own terminal exit scrubs it, §B4); every other exit is terminal.
                if not saw_suspended:
                    await drive.scrub_credentials()

    async def _astream_built(
        self,
        agent: Any,
        agent_input: Any,
        config: dict[str, Any],
        interrupt_on: dict[str, Any] | None,
        response_format: Any = None,
        structured_strategy: Any = None,
        park: ParkIdentity | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Project a built agent's run into contract events, then one terminal
        park/interrupt event per pending pause.

        The shared streaming core behind both faces, so a thread poisoned by an
        aborted turn is repaired here before the run. After the drive stops the
        pending interrupt is classified — an async-ask :class:`SuspendedFinal` park
        (whose durable index is persisted) or a HITL :class:`InterruptFinal` — by
        :func:`~tai42_agents._internal.park.finalize_drive`, which skips the state
        read entirely unless the run can pause (``interrupt_on`` set, or the run is
        park-capable and binds). The raw ``response_format`` is handed to the
        projection for validation; ``structured_strategy`` is the exact
        ``ToolStrategy`` the graph was compiled with, so the synthetic tool names it
        suppresses match by identity.
        """
        await _repair_dangling_tool_calls(agent, config)
        async for event in aproject_agent_events(
            agent, agent_input, config, response_format=response_format, structured_strategy=structured_strategy
        ):
            yield event
        for event in await finalize_drive(agent, config, interrupt_on, park):
            yield event

    async def _resolve_and_build(
        self,
        *,
        tools: list[StructuredTool],
        subagents: list[ResolvedSubAgentSpec],
        skills: list[str] | None,
        inline_skills: list[InlineSkill] | None,
        system_message: str,
        response_format: Any,
        interrupt_on: dict[str, Any] | None,
        llm_provider: str | None,
        checkpoint_provider: str | None,
        store_provider: str | None,
        llm_kwargs: dict[str, Any] | None,
        session: SandboxSession | None = None,
    ) -> Any:
        """Resolve the LLM / checkpointer / store from the registries and assemble
        the compiled deep agent. The caller builds the run config separately.

        ``session`` is the acquired durable sandbox session threaded through to the backend:
        set on a run/astream drive (scratch on the workspace VOLUME via
        ``SandboxSessionBackend``), ``None`` on the append path (the non-sandbox
        ``StateBackend`` — a checkpoint-only write needs no session, §B3.5)."""
        provider = llm_provider or llm_provider_settings().llm
        llm = await get_llm_async(provider=provider, **llm_settings().with_fallbacks(llm_kwargs or {}))

        cp_provider = checkpoint_provider or llm_provider_settings().checkpoint
        checkpointer = await checkpoint_registry().get_checkpointer(
            provider=cp_provider, conn_string=llm_provider_settings().checkpoint_conn_string
        )
        st_provider = store_provider or llm_provider_settings().store
        store = await store_registry().get_store(
            provider=st_provider, conn_string=llm_provider_settings().store_conn_string
        )

        return await build_langchain_deep_agent(
            llm=llm,
            store=store,
            checkpointer=checkpointer,
            tools=tools,
            skills=skills or None,
            inline_skills=inline_skills or None,
            system_prompt=system_message or None,
            interrupt_on=interrupt_on,
            response_format=response_format,
            subagents=subagents or None,
            session=session,
        )

    async def _build_agent(
        self,
        *,
        tools: list[StructuredTool],
        subagents: list[ResolvedSubAgentSpec],
        skills: list[str] | None,
        inline_skills: list[InlineSkill] | None,
        system_message: str,
        response_format: Any,
        interrupt_on: dict[str, Any] | None,
        thread_id: str | None,
        resume_checkpoint_id: str | None,
        llm_provider: str | None,
        checkpoint_provider: str | None,
        store_provider: str | None,
        llm_kwargs: dict[str, Any] | None,
        recursion_limit: int | None,
        langgraph_config: dict[str, Any] | None = None,
        session: SandboxSession | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Assemble the compiled deep agent and its run config for the streaming face.

        Wraps :meth:`_resolve_and_build` with the run config from :meth:`_run_config`,
        the same one the invoke face uses. The recursion cap bounds the TOP-LEVEL
        graph ONLY: each task-tool subagent runs its own graph bound by deepagents at
        9999, so the effective step budget is MULTIPLICATIVE across nesting depth.

        ``session`` is the acquired durable sandbox session the caller threads through to
        the backend (``None`` leaves the non-sandbox ``StateBackend`` default)."""
        agent = await self._resolve_and_build(
            tools=tools,
            subagents=subagents,
            skills=skills,
            inline_skills=inline_skills,
            system_message=system_message,
            response_format=response_format,
            interrupt_on=interrupt_on,
            llm_provider=llm_provider,
            checkpoint_provider=checkpoint_provider,
            store_provider=store_provider,
            llm_kwargs=llm_kwargs,
            session=session,
        )
        config = self._run_config(langgraph_config, thread_id, resume_checkpoint_id, recursion_limit)
        return agent, config

    @staticmethod
    def _rebuild_kwargs(
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
        """The JSON-serializable subset of a run's inputs that determines graph
        compilation — the identity a cross-worker resume recompiles the same graph from.

        Every DeepAgentInput value is a ``DeepAgentInput`` field name (subagents/inline_skills
        dumped to JSON, the system message already RENDERED so resume never re-renders
        differently), so :meth:`aresume_park` reconstructs the run inputs with
        ``ToolInput.model_validate``. The checkpoint provider and ``recursion_limit`` are pinned
        separately by :func:`~tai42_agents._internal.park.build_park_identity`, so they are
        deliberately absent here.

        ``workspace_key`` is the engine extra a cross-worker resume needs to REATTACH THE SAME
        durable volume (§B3.4); like ``recursion_limit`` it is NOT a ``DeepAgentInput`` field, so
        :meth:`aresume_park` pops it out before the JSON inputs validate."""
        return {
            "tool_names": list(tool_names),
            "subagents": [spec.model_dump(mode="json") for spec in (subagents or [])],
            "skills": list(skills) if skills else None,
            "inline_skills": [skill.model_dump(mode="json") for skill in inline_skills],
            "system_message": rendered_system,
            "interrupt_on": interrupt_on,
            "response_format": response_format,
            "llm_provider": llm_provider,
            "store_provider": store_provider,
            "llm_kwargs": llm_kwargs,
            "langgraph_config": langgraph_config,
            "workspace_key": workspace_key,
        }

    async def aresume_park(
        self,
        *,
        rebuild_kwargs: dict[str, Any],
        thread_id: str,
        resume_map: dict[str, dict[str, Any]],
    ) -> Any:
        """Rebuild the parked graph from its stored identity and resume its park interrupts BY
        ID with all answers — the deep-agent resume face the ``agent_resume`` continuation
        drives.

        ``resume_map`` is ``{interrupt_id: {interaction_id: answer}}`` over every park interrupt
        the super-step suspended on — one key for a single park, several for parallel subagent
        parks. Recompiles the same graph on the same ``thread_id`` + durable checkpoint provider
        from ``rebuild_kwargs``, asserts each stored park interrupt is still pending (else raises,
        so the caller releases the drive lease and the reaper redelivers), then drives one
        ``Command(resume=resume_map)`` under the drive wrapper — a re-park re-persists a fresh
        index (carrying the completion tool the driver rebound) and returns the suspended
        receipt, completion returns the final value.

        The LangGraph ``recursion_limit`` and the ``workspace_key`` are engine extras carried
        INSIDE ``rebuild_kwargs`` (the provider-free park identity holds no engine facts), popped
        out here before the JSON inputs validate against ``DeepAgentInput``. The ``workspace_key``
        REATTACHES THE SAME durable volume the park wrote (§B3.4), so a cross-worker resume drives
        the same scratch tree. Resume is a turn like any other: it reacquires the session, takes
        the shared workspace lease, and scrubs the bearer credential material on its own terminal
        exit."""
        rebuild = dict(rebuild_kwargs)
        recursion_limit = rebuild.pop("recursion_limit", None)
        workspace_key = rebuild.pop("workspace_key", None)
        validated = DeepAgentInput.model_validate(rebuild)
        response_format = validated.response_format
        interrupt_on = validated.interrupt_on
        strategy = as_tool_strategy(response_format)

        client_tools = (
            await tai42_app.tools.get_client_tools(list(validated.tool_names)) if validated.tool_names else []
        )
        internal_subagents = await resolve_subagent_specs(validated.subagents)
        coerced_inline_skills = [
            s if isinstance(s, InlineSkill) else InlineSkill(**s) for s in (validated.inline_skills or [])
        ]

        # Reattach the SAME durable volume by workspace_key and serialize this drive on the shared
        # workspace lease, exactly like a fresh turn (§B3.4): the lease wraps session-acquire +
        # cred-materialize + drive, so a concurrent resume for the same thread loses the lease
        # before opening a second session on the volume.
        async with DeepAgentSession.leased(thread_id=thread_id, workspace_key=workspace_key) as drive:
            suspended = False
            try:
                agent = await self._resolve_and_build(
                    tools=client_tools,
                    subagents=internal_subagents,
                    skills=validated.skills,
                    inline_skills=coerced_inline_skills or None,
                    system_message=validated.system_message or "",
                    response_format=strategy,
                    interrupt_on=interrupt_on,
                    llm_provider=validated.llm_provider,
                    checkpoint_provider=validated.checkpoint_provider,
                    store_provider=validated.store_provider,
                    llm_kwargs=validated.llm_kwargs,
                    session=drive.session,
                )
                config = self._run_config(validated.langgraph_config, thread_id, None, recursion_limit)

                snapshot = await agent.aget_state(config, subgraphs=True)
                pending_ids = {iid for iid, _ in _collect_pending_interrupts(snapshot)}
                missing = [interrupt_id for interrupt_id in resume_map if interrupt_id not in pending_ids]
                if missing:
                    if pending_ids:
                        # The graph is still parked, but not on every interrupt this super-step
                        # resumes — a corrupted routing, or a graph that advanced past one. Raise.
                        raise AgentResumeInterruptNotPendingError(next(iter(resume_map[missing[0]])), missing[0])
                    # No interrupt pending at all: the super-step already drove to a CLEAN TERMINAL,
                    # so this is an idempotent re-drive after a crash between the terminal drive and
                    # the index finalize. Re-produce the SAME terminal output from the persisted
                    # state — never re-invoke a resolved graph — so the completion handoff re-fires
                    # under its stable id and the delivery ledger dedupes it to one delivery.
                    if response_format is not None:
                        return extract_structured_output(snapshot.values, response_format)
                    return build_user_output(snapshot.values)

                # Bind again so a re-park on resume re-persists a fresh index (resume re-enters
                # through the agent's own bound entrypoint); the completion tool the driver
                # rebound is captured onto the new entry. No live tools on a rebuilt graph, so it is
                # park-capable by construction. The retention bound is min(checkpoint, workspace).
                park = build_park_identity(
                    agent_name=self.tool_name,
                    config=config,
                    checkpoint_provider=validated.checkpoint_provider,
                    has_live_tools=False,
                    rebuild_kwargs=rebuild_kwargs,
                    recursion_limit=recursion_limit,
                    completion_tool=get_park_completion_tool(),
                    bind=True,
                    extra_retention_horizon=drive.workspace_retention_horizon,
                )
                with park_continuation(park):
                    result = await self._drain(
                        self._astream_built(
                            agent,
                            Command(resume=resume_map),
                            config,
                            interrupt_on,
                            response_format=response_format,
                            structured_strategy=strategy,
                            park=park,
                        ),
                        response_format=response_format,
                    )
                suspended = _is_suspended_receipt(result)
                return result
            finally:
                if not suspended:
                    await drive.scrub_credentials()

    @staticmethod
    async def _pending_interrupts(agent: Any, config: dict[str, Any]) -> list[InterruptFinal]:
        """Read the interrupts a paused graph is waiting on, if any.

        An empty list means the run completed normally. A failure to read the
        snapshot propagates — a paused run whose interrupt we cannot read would
        otherwise hang invisibly.
        """
        snapshot = await agent.aget_state(config)
        interrupts = list(getattr(snapshot, "interrupts", None) or [])
        return [InterruptFinal(interrupt_id=intr.id, payload=intr.value) for intr in interrupts]
