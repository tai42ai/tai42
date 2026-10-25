"""The ``refine_agent`` — an Evaluator↔Critic refinement loop.

The Evaluator drafts, the Critic reviews, the Evaluator revises; the two alternate
until the Critic emits the approval token
(:data:`~tai42_agents.refine_agent.prompt.CRITIC_APPROVAL_MESSAGE`) or the
iteration budget is exhausted — which fails loudly with a ``RuntimeError`` rather
than returning an unapproved draft. An empty critic response also raises.

Only the final, approved evaluator pass streams; the negotiation runs silently in
the prelude (plain ``ainvoke`` per turn). ``run`` drains that same stream.

With a ``response_format`` set, the loop iterations stay text and only the final
approved answer is forced into the schema — run on a second, structured evaluator
fed the negotiation history explicitly on a fresh thread, surfaced as a terminal
:class:`~tai42_contract.agent.events.StructuredFinal`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from langchain.agents import create_agent
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tai42_contract.agent import AddressedMemory, Agent
from tai42_contract.agent.events import (
    RecursionLimitFinal,
    StreamEvent,
    StructuredFinal,
    StructuredOutputUnresolvedFinal,
)
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import TraceContext
from tai42_contract.template import TemplatedText
from tai42_kit.llm.checkpoint import mark_threads_finished
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.middleware import history_middlewares
from tai42_kit.llm.middleware.context_overflow import context_overflow_middlewares
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tai42_kit.llm.models import get_llm_async
from tai42_kit.llm.runtime import build_agent_input, build_system_message, build_user_output
from tai42_kit.llm.settings import llm_provider_settings, llm_settings
from tai42_kit.logging.settings import logging_settings
from tai42_kit.monitoring import begin_run_trace

from tai42_agents._internal.cache_mark import default_system_cache_mark
from tai42_agents._internal.config_util import (
    config_addressed,
    init_langgraph_config,
    start_run_thread,
    with_run_trace_lineage,
)
from tai42_agents._internal.lc_tools import structured_tools
from tai42_agents._internal.outcomes import outcome_for_drive_error
from tai42_agents._internal.park import bind_resume_per_step
from tai42_agents._internal.recovery import _repair_dangling_tool_calls, _tool_error_middleware
from tai42_agents._internal.reject import resolve_response_format
from tai42_agents._internal.render import render_message
from tai42_agents._internal.run_kwargs import guard_run_kwargs
from tai42_agents._internal.run_record import TerminalEvents, run_input
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import structured_output_stack
from tai42_agents.refine_agent.prompt import (
    CRITIC_APPROVAL_MESSAGE,
    CRITIC_SYSTEM_MESSAGE,
    EVALUATOR_SYSTEM_MESSAGE,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import StructuredTool
    from langgraph.checkpoint.base import BaseCheckpointSaver


# The contract ``Agent.run`` keys the refine loop has no seat for, mapped to the reason named in the
# raised error. The loop drives its Evaluator↔Critic negotiation through role-named parameters
# (evaluator_/critic_) and resolves tools from ``tool_names``.
_UNHONORED_REASONS: dict[str, str] = {
    "user_message": "the loop uses role-named evaluator_/critic_ messages and takes no user_message",
    "thread_id": (
        "thread_id is per-role RunnableConfig content set via evaluator_/critic_ langgraph_config's configurable; "
        "a single global thread_id would collide the two role graphs' checkpoint namespaces"
    ),
}

# The run kwarg that steers the run's own checkpoint continuity.
_CONTINUITY_KEYS = frozenset({"checkpoint_provider"})


def _final_evaluator_input() -> dict[str, Any]:
    """The prompt that asks the approved Evaluator for its final user-facing answer.

    Run against the same checkpointed thread the loop built up.
    """
    return {"messages": [{"role": "user", "content": "Critic Approved."}]}


async def _build_role_agent(
    llm: BaseChatModel,
    tools: Sequence[StructuredTool],
    system_prompt: str,
    checkpointer: BaseCheckpointSaver,
    *,
    provider: str,
    is_enabled_for_debug: bool,
    response_format: Any = None,
    structured_rail: Any = None,
) -> Any:
    """Build one role's ``create_agent`` stack — the Evaluator, the Critic, or the structured final Evaluator.

    Each role runs the same middleware stack: system purge first (so a stored system
    message never reaches the model), the context-overflow strategies fed this role's
    own system prompt (so the trimming budget covers the full outgoing request),
    ``provider``'s history middlewares (leading user turn, rolling cache mark), and
    tool-error visibility. Each role's system message is its
    graph's per-run ``system_prompt``, applied at the model-call boundary and never
    written into the checkpointed thread. Under the server-wide cache default it is
    marked with ``provider``'s system-prompt cache breakpoint, which the rolling
    cache-mark middleware exempts, so the stable prefix stays cacheable across turns.
    ``response_format`` forces the structured final answer on the final pass (``None``
    keeps the role text-shaped); ``structured_rail``, when present, is appended
    innermost so the final pass's payload is judged in-node under the per-run cap.
    """
    middleware: list[Any] = [
        SystemPurgeMiddleware(),
        *await context_overflow_middlewares(system_prompt=system_prompt),
        *history_middlewares(provider),
        _tool_error_middleware,
    ]
    if structured_rail is not None:
        middleware.append(structured_rail)
    extra = {} if response_format is None else {"response_format": response_format}
    return create_agent(
        llm,
        tools=list(tools),
        system_prompt=build_system_message(system_prompt, default_system_cache_mark(provider)),
        checkpointer=checkpointer,
        middleware=middleware,
        debug=is_enabled_for_debug,
        **extra,
    )


async def _run_refine_loop(
    tools: Sequence[StructuredTool],
    evaluator_message: TemplatedText | None,
    critic_message: TemplatedText | None,
    max_iterations: int,
    evaluator_llm_provider: str | None,
    critic_llm_provider: str | None,
    checkpoint_provider: str | None,
    evaluator_llm_kwargs: dict[str, Any] | None,
    critic_llm_kwargs: dict[str, Any] | None,
    evaluator_config: dict[str, Any] | None,
    critic_config: dict[str, Any] | None,
    run_context: TraceContext | None,
    response_format: Any = None,
    user_content_kwargs: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any], Any, list[str]]:
    """Run the Evaluator↔Critic loop to approval.

    Returns ``(final_agent, final_input, final_config, strategy, minted_threads)`` for the
    final approved evaluator pass the caller streams; ``strategy`` is the minted
    structured-output strategy the stream projection reads (``None`` when text-shaped), and
    ``minted_threads`` the role threads this run minted, which the caller marks finished at
    the run's end. A caller-supplied role thread leaves the checkpoint finished-thread
    ledger before the loop runs on it.

    ``run_context`` is the refine run's one trace lineage (its run-trace root's context): every
    role's config carries it, so the evaluator, the critic and the final pass all nest under the
    one run.

    ``user_content_kwargs`` (e.g. ``cache_control``) carries content-block keys onto
    the evaluator's first user turn — the run's primary caller-supplied message —
    where it persists in the checkpointed history the final pass re-sends.

    ``response_format`` (or ``None``): without one this is the loop's own evaluator
    resumed on its checkpointed thread with a "Critic Approved." prompt. With one, the
    final pass runs on a SECOND evaluator built with the capability-negotiated strategy
    and re-prompt rail (keyed on the evaluator model) on a FRESH thread, fed the
    negotiation history explicitly as input — never a checkpoint resume of the
    text-shaped loop thread into the structured graph.

    Raises ``RuntimeError`` when the Critic returns no feedback or ``max_iterations``
    is reached without approval — an unapproved draft is never returned.
    """
    rendered_evaluator_message = await render_message(evaluator_message, allow_empty=False, field="evaluator_message")
    rendered_critic_message = await render_message(critic_message, allow_empty=False, field="critic_message")

    evaluator_llm_provider = evaluator_llm_provider or llm_provider_settings().llm
    critic_llm_provider = critic_llm_provider or llm_provider_settings().llm

    evaluator_llm = await get_llm_async(
        provider=evaluator_llm_provider, **llm_settings().with_fallbacks(evaluator_llm_kwargs or {})
    )
    critic_llm = await get_llm_async(
        provider=critic_llm_provider, **llm_settings().with_fallbacks(critic_llm_kwargs or {})
    )

    checkpoint_provider = checkpoint_provider or llm_provider_settings().checkpoint
    checkpointer = await checkpoint_registry().get_checkpointer(
        provider=checkpoint_provider,
        conn_string=llm_provider_settings().checkpoint_conn_string,
    )
    is_enabled_for_debug = logging_settings().is_enabled_for("DEBUG")
    evaluator: Any = await _build_role_agent(
        evaluator_llm,
        tools,
        EVALUATOR_SYSTEM_MESSAGE,
        checkpointer,
        provider=evaluator_llm_provider,
        is_enabled_for_debug=is_enabled_for_debug,
    )
    critic: Any = await _build_role_agent(
        critic_llm,
        tools,
        CRITIC_SYSTEM_MESSAGE,
        checkpointer,
        provider=critic_llm_provider,
        is_enabled_for_debug=is_enabled_for_debug,
    )

    # One trace per refine run: every role's config is derived from the run's one lineage, so the
    # evaluator, the critic, and the final structured pass all nest under the one run. Each role
    # still gets its own thread_id and callbacks.
    evaluator_source = with_run_trace_lineage(evaluator_config, run_context)
    critic_source = with_run_trace_lineage(critic_config, run_context)
    evaluator_config = init_langgraph_config(evaluator_source, run_context)
    critic_config = init_langgraph_config(critic_source, run_context)
    minted_threads = [
        minted
        for minted in (
            await start_run_thread(evaluator_source, evaluator_config, provider=checkpoint_provider),
            await start_run_thread(critic_source, critic_config, provider=checkpoint_provider),
        )
        if minted is not None
    ]
    # The two role graphs run on their own checkpointed threads; repair either if an
    # aborted prior turn left it with dangling tool_calls before the loop resumes it.
    await _repair_dangling_tool_calls(evaluator, evaluator_config)
    await _repair_dangling_tool_calls(critic, critic_config)

    iteration = 0
    evaluator_input: dict[str, Any] = build_agent_input(
        rendered_evaluator_message, user_content_kwargs=user_content_kwargs
    )

    while iteration < max_iterations:
        evaluator_state = await evaluator.ainvoke(evaluator_input, evaluator_config)
        evaluator_output = build_user_output(evaluator_state)

        if iteration > 0:
            critic_input: dict[str, Any] = {"messages": [{"role": "user", "content": evaluator_output}]}
        else:
            critic_input = build_agent_input(rendered_critic_message, evaluator_output)

        critic_state = await critic.ainvoke(critic_input, critic_config)
        critic_output = build_user_output(critic_state)

        if not critic_output:
            raise RuntimeError("No critic feedback found.")

        if CRITIC_APPROVAL_MESSAGE in critic_output:
            break

        evaluator_input = {"messages": [{"role": "user", "content": critic_output}]}
        iteration += 1
    else:
        raise RuntimeError(f"Max iterations ({max_iterations}) reached without critic approval")

    if response_format is None:
        return evaluator, _final_evaluator_input(), evaluator_config, None, minted_threads

    # Force the final answer into the schema without re-shaping the loop thread:
    # feed its negotiation history as input to a second, structured evaluator on a
    # fresh thread (never a cross-topology resume). The strategy + rail are keyed on
    # the evaluator model, and the same strategy object is bound into both the final
    # pass and the stream projection.
    strategy, structured_rail = structured_output_stack(evaluator_llm, evaluator_llm_provider, response_format)
    snapshot = await evaluator.aget_state(evaluator_config)
    history = list(snapshot.values.get("messages", []))
    structured_evaluator: Any = await _build_role_agent(
        evaluator_llm,
        tools,
        EVALUATOR_SYSTEM_MESSAGE,
        checkpointer,
        provider=evaluator_llm_provider,
        is_enabled_for_debug=is_enabled_for_debug,
        response_format=strategy,
        structured_rail=structured_rail,
    )
    final_input = {"messages": [*history, {"role": "user", "content": "Critic Approved."}]}
    final_config = init_langgraph_config(with_run_trace_lineage(None, run_context), run_context)
    minted_threads.append(final_config["configurable"]["thread_id"])
    return structured_evaluator, final_input, final_config, strategy, minted_threads


def _require_structured_result(response_format: Any, *, saw_structured: bool, saw_outcome: bool) -> None:
    """Fail loudly when a requested ``response_format`` produced no ``StructuredFinal``.

    Checked after the stream drains. A run that ended on a typed non-fatal outcome (re-prompt cap or
    recursion limit) has that outcome as its result and passes.
    """
    if response_format is not None and not saw_structured and not saw_outcome:
        raise RuntimeError("agent run requested a response_format but produced no structured output")


class RefineAgentInput(BaseModel):
    """JSON tool-face parameters for ``refine_agent``.

    ``tool_names`` is a list of registered client-tool names (resolved to live tools at
    run time); the two role prompts are supplied inline or by template id.

    ``base_url``/``api_key`` in ``evaluator_llm_kwargs``/``critic_llm_kwargs``
    legitimately route to a caller-chosen model endpoint; expose any agent or tool
    carrying these kwargs only to trusted callers — an injected parent agent could
    redirect the model call to a hostile endpoint and leak the key/context.

    ``extra="forbid"`` rejects any unknown key loudly at validation rather than
    letting a typo at the run door vanish silently.
    """

    model_config = ConfigDict(extra="forbid")

    evaluator_message: TemplatedText | None = None
    critic_message: TemplatedText | None = None
    tool_names: list[str] | None = None
    max_iterations: int = 3
    response_format: TemplatedText | dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the forced structured output (needs a top-level 'title')."
    )
    user_content_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-block keys merged into the evaluator's first user message's text block (e.g. "
            "cache_control for Anthropic prompt caching). Provider-unknown keys surface as loud provider errors. "
            "On a checkpointed thread the model call keeps only the newest mark (older marks are "
            "stripped), so per-turn marking stays within the provider's per-request breakpoint cap."
        ),
    )
    evaluator_llm_provider: str | None = None
    critic_llm_provider: str | None = None
    checkpoint_provider: str | None = None
    evaluator_llm_kwargs: dict[str, Any] | None = None
    critic_llm_kwargs: dict[str, Any] | None = None
    evaluator_langgraph_config: dict[str, Any] | None = None
    critic_langgraph_config: dict[str, Any] | None = None

    @field_validator("user_content_kwargs")
    @classmethod
    def _empty_content_kwargs_is_unset(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """An empty dict carries no content-block keys — normalize {} to None.

        Reads as unset, matching the builders that treat {} as no mark.
        """
        return value or None


@tai42_app.agents.agent("refine_agent", tags={"agents"})
class RefineAgent(Agent):
    """Evaluator↔Critic refinement agent.

    ``astream`` runs the loop silently, then streams the final approved evaluator pass;
    ``run`` drains that stream.
    """

    tool_name: ClassVar[str] = "refine_agent"
    tool_description: ClassVar[str] = (
        "Run a refine workflow: an Evaluator LLM and a Critic LLM iterate until the "
        "Critic approves or max_iterations is reached."
    )
    ToolInput: ClassVar[type[BaseModel]] = RefineAgentInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        """The checkpoint provider when present, and the ids the evaluator and critic configs carry."""
        threads: set[str] = set()
        checkpoints: set[str] = set()
        for key in ("evaluator_langgraph_config", "critic_langgraph_config"):
            role_threads, role_checkpoints = config_addressed(run_kwargs.get(key))
            threads |= role_threads
            checkpoints |= role_checkpoints
        return AddressedMemory(
            keys=frozenset(_CONTINUITY_KEYS & set(run_kwargs)),
            thread_ids=frozenset(threads),
            checkpoint_ids=frozenset(checkpoints),
        )

    async def run(self, *, response_format: Any = None, **kwargs: Any) -> Any:
        """Run the loop and drain the final answer; ``astream``'s guard refuses an undeclared keyword.

        Returns the final approved answer by draining :meth:`astream`.

        A ``response_format`` is honored: its JSON-Schema dict must carry a top-level
        ``"title"``, and the drain returns the validated structured object the final
        approved pass forces, raising loudly if the run produced none.
        """
        resolved = await resolve_response_format("refine_agent", response_format)
        return await self._drain(self.astream(response_format=response_format, **kwargs), response_format=resolved)

    async def astream(
        self,
        *,
        evaluator_message: TemplatedText | None = None,
        critic_message: TemplatedText | None = None,
        tool_names: list[str] | None = None,
        max_iterations: int = 3,
        evaluator_llm_provider: str | None = None,
        critic_llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        evaluator_llm_kwargs: dict[str, Any] | None = None,
        critic_llm_kwargs: dict[str, Any] | None = None,
        evaluator_langgraph_config: dict[str, Any] | None = None,
        critic_langgraph_config: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        response_format: Any = None,
        thread_id: str | None = None,
        user_message: TemplatedText | None = None,
        **unknown: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the final evaluator pass; the Evaluator↔Critic loop runs silently until approval.

        The loop raises at ``max_iterations`` if approval never comes.

        ``tool_names``, ``checkpoint_provider``, ``response_format`` and
        ``user_content_kwargs`` are the honored contract parameters;
        ``user_content_kwargs`` merges content-block keys (e.g. ``cache_control``)
        onto the evaluator's first user turn, a provider-unknown key surfacing as a
        loud provider error. A set ``thread_id`` / ``user_message`` has no seat in the
        role-named loop and is refused loudly (see :data:`_UNHONORED_REASONS`), as is a
        keyword this face does not declare.

        With a ``response_format`` set, only the final approved answer is forced into
        the schema, surfaced as a terminal
        :class:`~tai42_contract.agent.events.StructuredFinal`; a final pass producing
        none raises loudly after the stream drains.
        """
        guard_run_kwargs(
            "refine_agent.astream",
            unknown,
            {
                "thread_id": thread_id,
                "user_message": user_message,
                "tool_names": tool_names or (),
                "response_format": response_format,
            },
            _UNHONORED_REASONS,
        )
        # The run's one trace root: current only inside each step (the loop, and each step of the final
        # pass's stream), never across a yield. It records the evaluator and critic messages, and the
        # terminal events when the stream ends.
        root = begin_run_trace(
            evaluator_langgraph_config,
            input_=run_input(evaluator_message=evaluator_message, critic_message=critic_message),
        )
        terminals = TerminalEvents()
        try:
            outcome = None
            with root.step():
                response_format = await resolve_response_format("refine_agent", response_format)
                resolved_tools = structured_tools(
                    await tai42_app.tools.get_client_tools(tool_names) if tool_names else []
                )
                try:
                    # The loop mints the structured-output strategy at the final evaluator pass
                    # (keyed on its model) and returns it; the same object is bound into both the
                    # final pass and the projection, so the synthetic tool names match by identity.
                    final_agent, final_input, final_config, strategy, minted_threads = await _run_refine_loop(
                        tools=resolved_tools,
                        evaluator_message=evaluator_message,
                        critic_message=critic_message,
                        max_iterations=max_iterations,
                        evaluator_llm_provider=evaluator_llm_provider,
                        critic_llm_provider=critic_llm_provider,
                        checkpoint_provider=checkpoint_provider,
                        evaluator_llm_kwargs=evaluator_llm_kwargs,
                        critic_llm_kwargs=critic_llm_kwargs,
                        evaluator_config=evaluator_langgraph_config,
                        critic_config=critic_langgraph_config,
                        run_context=root.context,
                        response_format=response_format,
                        user_content_kwargs=user_content_kwargs,
                    )
                except GraphRecursionError as exc:
                    # A recursion trip in an evaluator/critic turn ends the loop with the named,
                    # non-fatal outcome instead of a generic failure. Any other error propagates.
                    outcome = outcome_for_drive_error(exc, None)
                    if outcome is None:
                        raise
            if outcome is not None:
                yield terminals.note(outcome)
                root.record_output(terminals.events)
                return
            saw_structured = False
            saw_outcome = False
            async for event in bind_resume_per_step(
                root.step,
                aproject_agent_events(
                    final_agent,
                    final_input,
                    final_config,
                    response_format=response_format,
                    structured_strategy=strategy,
                ),
            ):
                if isinstance(event, StructuredFinal):
                    saw_structured = True
                elif isinstance(event, StructuredOutputUnresolvedFinal | RecursionLimitFinal):
                    saw_outcome = True
                yield terminals.note(event)
            _require_structured_result(response_format, saw_structured=saw_structured, saw_outcome=saw_outcome)
            # The run reached its terminal: every role thread it minted is finished.
            with root.step():
                await mark_threads_finished(minted_threads, provider=checkpoint_provider)
            root.record_output(terminals.events)
        except Exception as exc:
            root.fail(exc)
            raise
        finally:
            root.end()
