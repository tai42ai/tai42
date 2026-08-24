"""``tools_agent`` as an :class:`Agent`.

The plain/advanced LangGraph tools agent. Its three tool inputs are uniform —
``tool_names`` (client tools resolved through the app registry), live ``tools``,
and ``presets`` (a base tool bound to fixed kwargs, exposed as a callable tool) —
and are resolved together by :func:`resolve_tools`. A preset is any base tool bound to
fixed kwargs, e.g. ``base_tool="example_tool"`` with ``fixed_kwargs={"config": ...}``.

Two faces:

* :meth:`run` — the JSON tool-face. Resolves ``tool_names`` + ``presets`` through
  the app's tool facet, renders the system/user messages through the template
  manager, invokes the agent once, and returns the final answer as a string.
* :meth:`astream` — the in-process streaming face the API drives with live
  ``tools``. It projects the run into the contract ``StreamEvent`` taxonomy:
  :class:`ReasoningStep`, :class:`ToolCallStep` / :class:`ToolResultStep` pairs,
  :class:`MessageDelta`, :class:`MessageFinal`, :class:`RunUsage`, and — when the
  underlying run carries a structured response — :class:`StructuredFinal`. A
  requested ``response_format`` the run produces no structured response for
  raises after the stream drains rather than silently omitting the frame.

This agent wires no HITL approval interrupt (its ``langchain`` ``create_agent``
runtime has no ``interrupt_on``), so the stream never contains an
``InterruptFinal``. It CAN park, though: an async ``ask_user`` a tool raises parks
the run through the shared park middleware — the caller gets a suspended RECEIPT and
the durable index resumes it by id out of band, exactly as ``langchain_deep_agent`` does. A park
is recorded only when the run is park-capable AND a resume path is bound: the ``run``
face always binds one, and the ``astream`` face binds one only when a completion tool is
bound in context (the conversation turn binds one to deliver the resumed answer). With
no resume path bound an async ask refuses loudly pre-persist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tai42_contract.agent import Agent
from tai42_contract.agent.base import PresetSpec, SubAgentSpec
from tai42_contract.agent.events import StreamEvent, StructuredFinal, SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.interactions import get_park_completion
from tai42_kit.llm.runtime import build_user_output, extract_structured_output

from tai42_agents._internal.append import require_thread_id, to_thread_messages
from tai42_agents._internal.base_tool_agent import (
    ParkBuilder,
    _compile_tools_agent,
    _suspended_receipt,
    aappend_tools_agent_messages,
    ainvoke_tools_agent,
)
from tai42_agents._internal.config_util import build_run_config, init_langgraph_config
from tai42_agents._internal.park import (
    ParkIdentity,
    build_park_identity,
    finalize_drive,
    park_continuation,
    register_agent_resume_tool,
)
from tai42_agents._internal.park.driver import _collect_pending_interrupts
from tai42_agents._internal.park.errors import AgentResumeInterruptNotPendingError
from tai42_agents._internal.recovery import _repair_dangling_tool_calls
from tai42_agents._internal.reject import (
    reject_blank_memory_keys,
    reject_unhonored,
    reject_untitled_response_format,
)
from tai42_agents._internal.render import render_message
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents._internal.stream_events import astream_tools_agent_events

# ABC ``run`` parameters this agent's runtime cannot honor, mapped to the reason
# named in the raised error (the keys also define this agent's unhonored set).
# ``tools_agent`` runs a single LangGraph tools agent: no sub-agents, no
# composition strategy, no skill backend, no HITL approval interrupt, and no
# long-term store. ``response_format`` is NOT here — it forces the run's structured
# output through ``create_agent`` and is honored on both faces. ``recursion_limit``
# is NOT here either — it is a standard ``RunnableConfig`` key the compiled graph
# reads, so it is honored (overlaid onto the run config) rather than rejected.
# ``resume`` is NOT here either — an async ``ask_user`` park makes a run resumable,
# so a caller-driven ``Command(resume=...)`` is honored (the park itself resumes out
# of band through the ``agent_resume`` continuation).
_UNHONORED_REASONS: dict[str, str] = {
    "subagents": (
        "sub-agent delegation is langchain_deep_agent's domain; this agent never exposes sub-agents as callable tools"
    ),
    "strategy": "it applies no composition strategy and will not silently ignore one",
    "skills": "skill backends are langchain_deep_agent's domain; this agent loads none",
    "inline_skills": "skill backends are langchain_deep_agent's domain; this agent loads none",
    "interrupt_on": (
        "HITL approval interrupts are langchain_deep_agent's domain; this agent's create_agent runtime wires none "
        "(an async ask_user parks instead, resumed by id)"
    ),
    "store_provider": "this agent wires no long-term store",
}

# The unhonored parameters whose unset default is an empty sequence — a non-empty
# value is set. Every other unhonored parameter defaults to ``None`` and is set
# when not ``None`` (so a falsy-but-present value like ``resume=False`` still raises).
_UNHONORED_COLLECTION_PARAMS = frozenset({"subagents", "skills", "inline_skills"})


class ToolsAgentInput(BaseModel):
    """JSON tool-face parameters for ``tools_agent``.

    Live ``tools`` are absent from this JSON schema — they carry live objects a
    JSON caller cannot express. Both in-process faces (:meth:`ToolsAgent.run` and
    :meth:`ToolsAgent.astream`) accept them directly. ``presets`` bind base tools
    to fixed kwargs and are exposed to the model as callable tools (e.g. a flow
    graph).

    The schema advertises exactly the composable spec fields this agent's runtime
    honors — ``system_prompt``, ``tool_names``, ``presets``, ``response_format`` —
    so an authored agent (``ToolsAgent.spec_runnable = True``) bakes only fields
    this agent actually applies. The composable fields the runtime cannot honor
    (``subagents``, ``strategy``) are absent from the schema, and
    ``extra="forbid"`` rejects them — and any other unknown key — loudly at
    validation rather than silently ignoring them. :meth:`ToolsAgent.run` /
    :meth:`ToolsAgent.astream` carry a matching defense-in-depth raise for
    in-process callers that bypass this validation.

    ``response_format`` is a JSON-Schema dict with a required top-level ``"title"``
    (used as the structured-output name); when set, the run forces the model to
    emit output matching it and returns the validated structured object.

    ``base_url``/``api_key`` in ``llm_kwargs`` legitimately route to a caller-chosen
    model endpoint; expose any agent or tool carrying these kwargs only to trusted
    callers — an injected parent agent could redirect the model call to a hostile
    endpoint and leak the key/context.
    """

    model_config = ConfigDict(extra="forbid")

    tool_names: list[str] = Field(default_factory=list, description="Client tool names to load.")
    presets: list[PresetSpec] | None = Field(
        default=None,
        description="Base tools bound to fixed kwargs, exposed as callable tools (e.g. a flow graph).",
    )
    system_prompt: str | None = Field(
        default=None,
        description="System prompt baked into an authored agent (mapped to the system_message run kwarg).",
    )
    response_format: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the forced structured output (needs a top-level 'title')."
    )
    system_content_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-block keys merged into the system message's text block (e.g. cache_control "
            "for Anthropic prompt caching). Provider-unknown keys surface as loud provider errors."
        ),
    )
    user_content_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-block keys merged into the last user message's text block (e.g. cache_control "
            "for Anthropic prompt caching). Provider-unknown keys surface as loud provider errors. "
            "On a checkpointed thread the model call keeps only the newest mark (older marks are "
            "stripped), so per-turn marking stays within the provider's breakpoint cap (Anthropic: 4)."
        ),
    )
    system_message: str | None = ""
    user_message: str | None = ""
    system_message_id: str | None = ""
    user_message_id: str | None = ""
    system_message_kwargs: dict[str, Any] | None = None
    user_message_kwargs: dict[str, Any] | None = None
    llm_provider: str | None = None
    checkpoint_provider: str | None = None
    llm_kwargs: dict[str, Any] | None = None
    langgraph_config: dict[str, Any] | None = None

    @field_validator("system_content_kwargs", "user_content_kwargs")
    @classmethod
    def _empty_content_kwargs_is_unset(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """An empty dict carries no content-block keys — normalize {} to None so it
        reads as unset, matching the builders that treat {} as no mark."""
        return value or None


# A parking agent must bind the hidden ``agent_resume`` continuation from its OWN
# registration: the park package no longer binds it as a module-import side effect, so a box
# loading this agent (even with no deep engine present) still resumes its async parks. The
# call is per-epoch idempotent, so a combined box binds it exactly once.
register_agent_resume_tool()


@tai42_app.agents.agent("tools_agent", tags={"agents"})
class ToolsAgent(Agent):
    tool_name: ClassVar[str] = "tools_agent"
    tool_description: ClassVar[str] = (
        "Create and run a LangGraph tools agent. Loads tools by name (and optional "
        "presets) and runs with the given system/user messages."
    )
    ToolInput: ClassVar[type[BaseModel]] = ToolsAgentInput
    spec_runnable: ClassVar[bool] = True

    @classmethod
    def from_tool_input(cls, validated: BaseModel) -> dict[str, Any]:
        """Map a validated :class:`ToolsAgentInput` to ``run``/``astream`` kwargs.

        Extends the base pass-through with this agent's one field rename: the
        composable ``system_prompt`` spec field becomes the ``system_message``
        run kwarg. Every other set field passes through unchanged.

        ``system_prompt`` and ``system_message`` both define the system message, so
        supplying a NON-EMPTY ``system_message`` alongside ``system_prompt`` is a
        conflict — it is rejected loudly rather than silently dropping one. (This is
        what stops a run request's ``system_message`` from silently overriding an
        authored agent's baked ``system_prompt``, which maps to the same run kwarg.) An
        empty ``system_message`` — the field default the tool-face wrapper materializes
        — is not a real value and is simply superseded by the mapped ``system_prompt``.
        """
        run_kwargs = super().from_tool_input(validated)
        if "system_prompt" in run_kwargs:
            if run_kwargs.get("system_message"):
                raise ValueError(
                    "set only one of system_prompt or system_message: both define the system "
                    "message (system_prompt is the composable spec field mapped to system_message)"
                )
            run_kwargs["system_message"] = run_kwargs.pop("system_prompt")
        return run_kwargs

    async def run(
        self,
        *,
        tools: Sequence[StructuredTool] = (),
        tool_names: Sequence[str] = (),
        presets: Sequence[PresetSpec] | None = None,
        subagents: Sequence[SubAgentSpec] | None = None,
        strategy: str | None = None,
        response_format: Any = None,
        interrupt_on: dict[str, Any] | None = None,
        skills: Sequence[str] = (),
        inline_skills: Sequence[dict[str, Any]] = (),
        recursion_limit: int | None = None,
        resume: Any = None,
        store_provider: str | None = None,
        system_message: str = "",
        user_message: str = "",
        system_message_id: str = "",
        user_message_id: str = "",
        system_message_kwargs: dict[str, Any] | None = None,
        user_message_kwargs: dict[str, Any] | None = None,
        system_content_kwargs: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        thread_id: str | None = None,
        resume_checkpoint_id: str | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        """Resolve tools, render the messages, run once, return the final value.

        ``tools`` (live objects), ``tool_names``, and ``presets`` resolve together
        through the app's tool facet — the SAME resolution :meth:`astream` applies;
        the system/user messages render through the template manager (literal
        content or a stored template id); ``thread_id`` (the LangGraph memory key)
        and ``resume_checkpoint_id`` (the checkpoint a run forks from) map into the
        run config's ``configurable``. The resolved tools, rendered messages, and
        config drive a single agent invocation whose result is returned.

        ``response_format`` forces the run's structured output: when set, the model
        is constrained to emit output matching the schema and the validated
        structured object is returned instead of the text — a JSON-Schema dict must
        carry a top-level ``"title"`` (the structured-output name). A requested
        ``response_format`` that the run produces none for raises loudly rather than
        silently falling back to text.

        ``recursion_limit`` (a standard ``RunnableConfig`` key the compiled graph
        reads) overlays the run config's top level, so a caller's step bound reaches
        the graph. ``system_content_kwargs`` / ``user_content_kwargs`` merge
        content-block keys (e.g. ``cache_control``) onto the system message and the
        last user message; a key the provider does not know surfaces as a loud
        provider error rather than a silent drop. The ABC ``run`` parameters this
        agent's runtime cannot honor are rejected loudly rather than silently
        dropped: ``subagents`` (sub-agent delegation is langchain_deep_agent's domain —
        tools_agent never exposes them as callable tools), ``strategy`` (no
        composition strategy is applied), ``skills`` / ``inline_skills`` (skill
        backends are langchain_deep_agent's domain), ``interrupt_on`` (no HITL approval
        interrupt is wired), and ``store_provider`` (no long-term store is wired).

        Provide exactly one of ``user_message`` (a fresh turn) or ``resume``
        (answering a prior async-ask park with ``Command(resume=...)``). A run that
        parks on an async ``ask_user`` returns a suspended RECEIPT
        (``{"status": "suspended", ...}``) instead of an answer and resumes out of
        band. This face binds no completion tool, so a resumed run's final text is
        delivered nowhere — its side effects are the product; a caller needing the
        answer must invoke through a completion-bound door. A run carrying live
        ``tools``, a non-durable checkpoint, or an unconfigured park index is not
        park-capable and its async ask refuses loudly pre-persist.
        """
        reject_unhonored(
            "tools_agent.run",
            {
                "subagents": subagents,
                "strategy": strategy,
                "interrupt_on": interrupt_on,
                "skills": skills,
                "inline_skills": inline_skills,
                "store_provider": store_provider,
            },
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys("tools_agent.run", thread_id=thread_id, resume_checkpoint_id=resume_checkpoint_id)
        reject_untitled_response_format("tools_agent", response_format)
        if resume is None and not (user_message or user_message_id):
            raise ValueError("tools_agent.run requires exactly one of user_message or resume")
        if resume is not None:
            if user_message or user_message_id:
                raise ValueError("tools_agent.run requires exactly one of user_message or resume, not both.")
            if user_content_kwargs:
                raise ValueError(
                    "tools_agent.run: user_content_kwargs applies to a fresh user_message turn, not a resume"
                )
        resolved_tools = await resolve_tools(tai42_app.tools, list(tool_names), list(tools), list(presets or []))
        rendered_system = await render_message(
            system_message,
            system_message_id,
            system_message_kwargs,
        )
        rendered_user = (
            "" if resume is not None else await render_message(user_message, user_message_id, user_message_kwargs)
        )
        config = build_run_config(langgraph_config, thread_id, resume_checkpoint_id, recursion_limit)

        # The run face returns the park RECEIPT to its caller (the continuation returns
        # the resumed result), so it binds the resume continuation. The park identity is
        # built against the FINAL thread-id-resolved config (deferred through the
        # builder), so a keyless run's minted thread id is captured.
        def build_park(final_config: dict[str, Any]) -> ParkIdentity | None:
            return build_park_identity(
                agent_name=self.tool_name,
                config=final_config,
                checkpoint_provider=checkpoint_provider,
                has_live_tools=bool(tools),
                rebuild_kwargs=self._rebuild_kwargs(
                    tool_names=tool_names,
                    presets=presets,
                    rendered_system=rendered_system,
                    system_content_kwargs=system_content_kwargs,
                    response_format=response_format,
                    llm_provider=llm_provider,
                    llm_kwargs=llm_kwargs,
                    langgraph_config=langgraph_config,
                ),
                recursion_limit=recursion_limit,
                bind=True,
            )

        result = await ainvoke_tools_agent(
            system_message=rendered_system,
            user_message=[rendered_user],
            tools=resolved_tools,
            llm_provider=llm_provider,
            checkpoint_provider=checkpoint_provider,
            llm_kwargs=llm_kwargs,
            config=config,
            system_content_kwargs=system_content_kwargs,
            user_content_kwargs=user_content_kwargs,
            response_format=response_format,
            park_builder=build_park,
            resume=resume,
        )
        if result.suspended is not None:
            return result.suspended
        if response_format is not None:
            # ainvoke_tools_agent already validated the structured output and
            # raised on a requested-but-missing one.
            return result.structured
        return result.output

    async def astream(
        self,
        *,
        tools: Sequence[StructuredTool] = (),
        tool_names: Sequence[str] = (),
        presets: Sequence[PresetSpec] | None = None,
        subagents: Sequence[SubAgentSpec] | None = None,
        strategy: str | None = None,
        response_format: Any = None,
        interrupt_on: dict[str, Any] | None = None,
        skills: Sequence[str] = (),
        inline_skills: Sequence[dict[str, Any]] = (),
        recursion_limit: int | None = None,
        resume: Any = None,
        store_provider: str | None = None,
        system_message: str = "",
        user_message: str = "",
        system_message_id: str = "",
        user_message_id: str = "",
        system_message_kwargs: dict[str, Any] | None = None,
        user_message_kwargs: dict[str, Any] | None = None,
        system_content_kwargs: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        thread_id: str | None = None,
        resume_checkpoint_id: str | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Run one turn and yield the contract event stream.

        ``tools`` (live objects), ``tool_names`` (names resolved through the app
        facet), and ``presets`` resolve together into the callable tool set — the
        SAME resolution :meth:`run` applies — so a baked ``tool_names`` / ``presets``
        spec drives the streamed run instead of being dropped. The system/user
        messages render through the template manager — the SAME rendering
        :meth:`run` applies — so a stored template id reaches the streamed run as
        its rendered content. ``thread_id`` is the LangGraph memory key;
        ``resume_checkpoint_id`` forks past an aborted turn (the conversation's last
        completed checkpoint). ``langgraph_config`` is the base run config: a
        ``configurable.thread_id`` (or ``checkpoint_id``) it carries directly is
        honored here — in parity with :meth:`run`, through the one shared
        :func:`~tai42_agents._internal.config_util.build_run_config` helper — so a
        caller pinning a thread over this face resumes its checkpointed memory.
        A keyless run pins no ``thread_id`` at all, so the run config helper mints a
        fresh isolated one per run rather than collapsing every keyless run onto one
        shared checkpoint thread.

        ``response_format`` forces the run's structured output — in parity with
        :meth:`run` — surfaced as a terminal :class:`StructuredFinal`; a JSON-Schema
        dict must carry a top-level ``"title"`` (the structured-output name), and a
        requested ``response_format`` the run produces none for raises loudly after
        the stream drains rather than silently omitting the frame.
        ``recursion_limit`` (a standard ``RunnableConfig`` key the compiled graph
        reads) overlays the run config's top level, in parity with :meth:`run`, so a
        caller's step bound reaches the graph. ``system_content_kwargs`` /
        ``user_content_kwargs`` merge content-block keys (e.g. ``cache_control``)
        onto the system message and the last user message, in parity with
        :meth:`run`; a key the provider does not know surfaces as a loud provider
        error rather than a silent drop. The ABC parameters this agent's
        runtime cannot honor are rejected loudly here too — in parity with
        :meth:`run` — rather than silently dropped: ``subagents`` (delegation is
        langchain_deep_agent's domain), ``strategy`` (no composition strategy is applied),
        ``skills`` / ``inline_skills`` (skill backends are langchain_deep_agent's domain),
        ``interrupt_on`` (no HITL approval interrupt is wired), and
        ``store_provider`` (no long-term store is wired).

        This streaming face binds a resume path — and so lets an async ``ask_user``
        park — ONLY when a completion tool is bound around the run (the conversation
        turn binds one to deliver a resumed answer back to the originating thread).
        With none bound (a direct SSE run), an async ask refuses loudly pre-persist:
        the streaming caller has no out-of-band delivery path for a late answer.
        ``resume`` drives ``Command(resume=...)`` — answering a prior park — in place
        of a fresh ``user_message``.
        """
        reject_unhonored(
            "tools_agent.astream",
            {
                "subagents": subagents,
                "strategy": strategy,
                "interrupt_on": interrupt_on,
                "skills": skills,
                "inline_skills": inline_skills,
                "store_provider": store_provider,
            },
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys("tools_agent.astream", thread_id=thread_id, resume_checkpoint_id=resume_checkpoint_id)
        reject_untitled_response_format("tools_agent", response_format)
        if resume is not None and (user_message or user_message_id):
            raise ValueError("tools_agent.astream requires exactly one of user_message or resume, not both.")
        resolved_tools = await resolve_tools(tai42_app.tools, list(tool_names), list(tools), list(presets or []))
        rendered_system = await render_message(system_message, system_message_id, system_message_kwargs)
        rendered_user = (
            "" if resume is not None else await render_message(user_message, user_message_id, user_message_kwargs)
        )
        config = build_run_config(langgraph_config, thread_id, resume_checkpoint_id, recursion_limit)
        park_builder = self._astream_park_builder(
            tool_names=tool_names,
            tools=tools,
            presets=presets,
            rendered_system=rendered_system,
            system_content_kwargs=system_content_kwargs,
            response_format=response_format,
            checkpoint_provider=checkpoint_provider,
            llm_provider=llm_provider,
            llm_kwargs=llm_kwargs,
            langgraph_config=langgraph_config,
            recursion_limit=recursion_limit,
        )
        saw_structured = False
        async for event in astream_tools_agent_events(
            system_message=rendered_system,
            user_message=[rendered_user],
            tools=resolved_tools,
            llm_provider=llm_provider,
            checkpoint_provider=checkpoint_provider,
            llm_kwargs=llm_kwargs,
            config=config,
            system_content_kwargs=system_content_kwargs,
            user_content_kwargs=user_content_kwargs,
            response_format=response_format,
            park_builder=park_builder,
            resume=resume,
        ):
            if isinstance(event, StructuredFinal):
                saw_structured = True
            yield event
        # Structured-final parity with the invoke face: a requested response_format
        # that produced no StructuredFinal fails loudly after the stream drains
        # rather than silently omitting the frame.
        if response_format is not None and not saw_structured:
            raise RuntimeError("agent run requested a response_format but produced no structured output")

    async def append_thread_messages(
        self,
        *,
        thread_id: str,
        messages: list[dict[str, str]],
        llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        llm_kwargs: dict[str, Any] | None = None,
        langgraph_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        """Append ``messages`` to the thread's stored history without running the model.

        ``messages`` items are ``{"role": "user"|"assistant", "content": str}`` — an
        unknown role or blank content raises. ``thread_id`` (or a
        ``configurable.thread_id`` carried in ``langgraph_config``) names the thread
        to append to; a missing one raises rather than minting a fresh thread the
        messages would be lost on. ``llm_provider`` / ``checkpoint_provider`` /
        ``llm_kwargs`` resolve the same model and checkpointer a run would, so the
        append lands on the SAME checkpointed thread a run reads.
        """
        converted = to_thread_messages(messages)
        reject_blank_memory_keys("tools_agent.append_thread_messages", thread_id=thread_id, resume_checkpoint_id=None)
        config = build_run_config(langgraph_config, thread_id)
        require_thread_id("tools_agent.append_thread_messages", config)
        await aappend_tools_agent_messages(
            converted,
            config,
            llm_provider=llm_provider,
            checkpoint_provider=checkpoint_provider,
            llm_kwargs=llm_kwargs,
        )

    @staticmethod
    def _rebuild_kwargs(
        *,
        tool_names: Sequence[str],
        presets: Sequence[PresetSpec] | None,
        rendered_system: str,
        system_content_kwargs: dict[str, Any] | None,
        response_format: Any,
        llm_provider: str | None,
        llm_kwargs: dict[str, Any] | None,
        langgraph_config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The JSON-serializable subset of a run's inputs that determines graph
        compilation — the identity a cross-worker resume recompiles the same graph from.

        Every value is a ``ToolsAgentInput`` field name (presets dumped to JSON, the
        system message already RENDERED so a resume never re-renders differently), so
        :meth:`aresume_park` reconstructs the run inputs with ``ToolInput.model_validate``.
        The checkpoint provider is pinned separately by
        :func:`~tai42_agents._internal.park.build_park_identity` (the resolved durable
        one), so it is deliberately absent here."""
        return {
            "tool_names": list(tool_names),
            "presets": [spec.model_dump(mode="json") for spec in (presets or [])],
            "system_message": rendered_system,
            "system_content_kwargs": system_content_kwargs,
            "response_format": response_format,
            "llm_provider": llm_provider,
            "llm_kwargs": llm_kwargs,
            "langgraph_config": langgraph_config,
        }

    def _astream_park_builder(
        self,
        *,
        tool_names: Sequence[str],
        tools: Sequence[StructuredTool],
        presets: Sequence[PresetSpec] | None,
        rendered_system: str,
        system_content_kwargs: dict[str, Any] | None,
        response_format: Any,
        checkpoint_provider: str | None,
        llm_provider: str | None,
        llm_kwargs: dict[str, Any] | None,
        langgraph_config: dict[str, Any] | None,
        recursion_limit: int | None,
    ) -> ParkBuilder | None:
        """The park builder for the streaming face, or ``None`` when no resumed-answer
        delivery path is bound.

        The streaming face returns its stream to a caller that cannot receive a late
        answer, so it binds a resume path — and lets an async ask park — ONLY when a
        completion tool is bound in context (the conversation turn binds one). That
        completion tool is stored on the park entry and fired with the final answer on a
        clean terminal drive. With none bound, this returns ``None`` so an async ask
        refuses loudly pre-persist.

        An agent delivers through the bridge thread it runs under, so it keeps only the
        completion tool name and ignores the opaque completion context."""
        completion_tool, _ = get_park_completion()
        if completion_tool is None:
            return None

        def build(final_config: dict[str, Any]) -> ParkIdentity | None:
            return build_park_identity(
                agent_name=self.tool_name,
                config=final_config,
                checkpoint_provider=checkpoint_provider,
                has_live_tools=bool(tools),
                rebuild_kwargs=self._rebuild_kwargs(
                    tool_names=tool_names,
                    presets=presets,
                    rendered_system=rendered_system,
                    system_content_kwargs=system_content_kwargs,
                    response_format=response_format,
                    llm_provider=llm_provider,
                    llm_kwargs=llm_kwargs,
                    langgraph_config=langgraph_config,
                ),
                recursion_limit=recursion_limit,
                completion_tool=completion_tool,
                bind=True,
            )

        return build

    async def aresume_park(
        self,
        *,
        rebuild_kwargs: dict[str, Any],
        thread_id: str,
        resume_map: dict[str, dict[str, Any]],
    ) -> Any:
        """Rebuild the parked graph from its stored identity and resume its park interrupts BY
        ID with all answers — the tools-agent resume face the ``agent_resume`` continuation
        drives.

        ``resume_map`` is ``{interrupt_id: {interaction_id: answer}}`` over every park interrupt
        the super-step suspended on — one key for a single park, several for parallel subagent
        parks. Recompiles the same graph on the same ``thread_id`` + durable checkpoint provider
        from ``rebuild_kwargs``, asserts each stored park interrupt is still pending (else raises,
        so the caller releases the drive lease and the reaper redelivers), then drives one
        ``Command(resume=resume_map)`` under the drive wrapper — a re-park re-persists a fresh
        index (carrying the completion tool the driver rebound) and returns the suspended
        receipt, completion returns the final value.

        The LangGraph ``recursion_limit`` is an engine extra carried INSIDE ``rebuild_kwargs``
        (the provider-free park identity holds no engine facts), popped out here before the
        JSON inputs validate against ``ToolsAgentInput``."""
        rebuild = dict(rebuild_kwargs)
        recursion_limit = rebuild.pop("recursion_limit", None)
        validated = ToolsAgentInput.model_validate(rebuild)
        resolved_tools = await resolve_tools(
            tai42_app.tools, list(validated.tool_names), [], list(validated.presets or [])
        )
        agent = await _compile_tools_agent(
            resolved_tools,
            llm_provider=validated.llm_provider,
            checkpoint_provider=validated.checkpoint_provider,
            llm_kwargs=validated.llm_kwargs,
            response_format=validated.response_format,
            system_message=validated.system_message or "",
            system_content_kwargs=validated.system_content_kwargs,
        )
        config = init_langgraph_config(build_run_config(validated.langgraph_config, thread_id, None, recursion_limit))
        await _repair_dangling_tool_calls(agent, config)
        snapshot = await agent.aget_state(config, subgraphs=True)
        pending_ids = {iid for iid, _ in _collect_pending_interrupts(snapshot)}
        missing = [interrupt_id for interrupt_id in resume_map if interrupt_id not in pending_ids]
        if missing:
            if pending_ids:
                # The graph is still parked, but not on every interrupt this super-step resumes
                # — a corrupted routing, or a graph that advanced past one. Raise.
                raise AgentResumeInterruptNotPendingError(next(iter(resume_map[missing[0]])), missing[0])
            # No interrupt pending at all: the super-step already drove to a CLEAN TERMINAL, so
            # this is an idempotent re-drive after a crash between the terminal drive and the
            # index finalize. Re-produce the SAME terminal output from the persisted state —
            # never re-invoke a resolved graph — so the completion handoff re-fires under its
            # stable id and the delivery ledger dedupes it to one delivery.
            if validated.response_format is not None:
                return extract_structured_output(snapshot.values, validated.response_format)
            return build_user_output(snapshot.values)

        # Bind again so a re-park on resume re-persists a fresh index (resume re-enters
        # through the agent's own bound entrypoint); the completion tool the driver
        # rebound is captured onto the new entry. No live tools on a rebuilt graph, so it is
        # park-capable by construction.
        completion_tool, _ = get_park_completion()
        park = build_park_identity(
            agent_name=self.tool_name,
            config=config,
            checkpoint_provider=validated.checkpoint_provider,
            has_live_tools=False,
            rebuild_kwargs=rebuild_kwargs,
            recursion_limit=recursion_limit,
            completion_tool=completion_tool,
            bind=True,
        )
        with park_continuation(park):
            state = await agent.ainvoke(Command(resume=resume_map), config)
            events = await finalize_drive(agent, config, None, park)
        for event in events:
            if isinstance(event, SuspendedFinal):
                return _suspended_receipt(event)
        if validated.response_format is not None:
            return extract_structured_output(state, validated.response_format)
        return build_user_output(state)
