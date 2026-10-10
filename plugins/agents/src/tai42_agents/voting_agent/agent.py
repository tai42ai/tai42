"""The voting agent — N voter LLMs answer in parallel, one judge LLM decides.

Each ``VoterSpec`` in ``voters`` spawns one voter; a list lets the same provider
appear more than once and each voter pin its own model. Every voter answers the
same rendered ``voter_message`` in parallel, then the judge receives the rendered
``judge_message`` followed by one line per voter (provider, model, verdict) and
decides by majority vote, breaking ties with its own reasoning.

Only the judge streams; the voters run silently in the prelude and their verdicts
are collected before the judge starts. ``astream`` emits the judge's per-step
events and ends with a terminal ``StructuredFinal`` carrying the full
``VotingOutput``; ``run`` drains that stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, field_validator
from tai42_contract.agent import AddressedMemory, Agent
from tai42_contract.agent.events import MessageFinal, RunUsage, StreamEvent, StructuredFinal
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.llm import resolve_trace_context
from tai42_kit.llm.settings import llm_provider_settings

from tai42_agents._internal.base_tool_agent import ainvoke_tools_agent
from tai42_agents._internal.config_util import config_addressed, with_run_trace_lineage
from tai42_agents._internal.graph_cache import ToolsAgentGraph, ToolsAgentGraphSpec, tools_agent_graph
from tai42_agents._internal.render import render_message
from tai42_agents._internal.run_kwargs import guard_run_kwargs
from tai42_agents._internal.stream_events import astream_tools_agent_events
from tai42_agents.settings import agents_limits_settings
from tai42_agents.voting_agent.model import VoteInfo, VoterSpec, VotingOutput
from tai42_agents.voting_agent.prompt import JUDGE_SYSTEM_MESSAGE, VOTER_SYSTEM_MESSAGE


async def _run_voters(
    judge_message: TemplatedText | None,
    voter_message: TemplatedText | None,
    judge_llm_provider: str,
    judge_llm_kwargs: dict[str, Any],
    voters: list[VoterSpec] | None,
    voter_tools: list[str] | None,
    checkpoint_provider: str | None,
    voter_config: dict[str, Any] | None,
) -> tuple[list[str], list[VoteInfo]]:
    """Render the messages, run every voter in parallel, and assemble the judge's input.

    Returns ``(user_messages, voters_info)``: ``user_messages`` is the rendered judge
    message followed by one line per voter. When ``voters`` is empty, a single voter runs
    on the judge's (already defaulted) provider. Every voter's graph is built before any
    voter runs, so an unknown voter tool raises before any voter LLM is called.

    Each ``VoteInfo.model`` records the model the voter actually ran on when known,
    else the model its spec pinned, else ``None`` — never a placeholder.
    """
    rendered_judge_message: str = await render_message(judge_message, allow_empty=False, field="judge_message")
    rendered_voter_message: str = await render_message(voter_message, allow_empty=False, field="voter_message")

    voters = voters or [VoterSpec(provider=judge_llm_provider, llm_kwargs=judge_llm_kwargs or None)]

    limits = agents_limits_settings()
    if len(voters) > limits.max_voters:
        raise ValueError(
            f"voting_agent got {len(voters)} voters; the limit is {limits.max_voters} (TAI_AGENTS_MAX_VOTERS)"
        )

    graphs = [
        await tools_agent_graph(
            ToolsAgentGraphSpec(
                tool_names=tuple(voter_tools or ()),
                system_message=VOTER_SYSTEM_MESSAGE,
                llm_provider=spec.provider,
                llm_kwargs=spec.effective_llm_kwargs(),
                checkpoint_provider=checkpoint_provider,
            )
        )
        for spec in voters
    ]
    sem = asyncio.Semaphore(limits.voter_concurrency)

    async def _run_one(graph: ToolsAgentGraph) -> Any:
        async with sem:
            return await ainvoke_tools_agent(graph=graph, user_message=[rendered_voter_message], config=voter_config)

    results = await asyncio.gather(*(_run_one(graph) for graph in graphs))

    voters_info = [
        VoteInfo(
            provider=spec.provider,
            model=results[i].usage.model or spec.declared_model,
            verdict=results[i].output,
        )
        for i, spec in enumerate(voters)
    ]

    voter_messages = [
        f"Provider: {info.provider}, Model: {info.model}, Verdict: {info.verdict}" for info in voters_info
    ]
    user_messages = [rendered_judge_message, *voter_messages]
    return user_messages, voters_info


# The contract ``Agent.run`` keys the voting runtime has no seat for, mapped to the reason named in
# the raised error. Voting drives its judge and voters through role-named params (``judge_*``/
# ``voter_*``) plus ``checkpoint_provider``. ``response_format`` is conditional: ``VotingOutput``
# (the type this agent always produces) is accepted, any other value is refused.
_UNHONORED_REASONS: dict[str, str] = {
    "tool_names": "it drives its judge and voters through role-named judge_tools/voter_tools",
    "user_message": "it renders role-named judge_message/voter_message and takes no user_message",
    "thread_id": (
        "thread_id is per-role RunnableConfig content set via judge_/voter_ langgraph_config's configurable; "
        "a single global thread_id would collide the two role graphs' checkpoint namespaces"
    ),
    "response_format": (
        "it always produces a VotingOutput and cannot honor a different structured output type; "
        "pass VotingOutput or omit it"
    ),
}

# The run kwarg that steers the run's own checkpoint continuity.
_CONTINUITY_KEYS = frozenset({"checkpoint_provider"})


class VotingAgentInput(BaseModel):
    """JSON tool-face params.

    ``judge_tools`` / ``voter_tools`` are client-tool names resolved to live tools at run time
    (live tools are API-only via ``astream`` and are not part of this JSON shape).

    ``base_url``/``api_key`` in ``judge_llm_kwargs`` legitimately route to a
    caller-chosen model endpoint; expose any agent or tool carrying these kwargs
    only to trusted callers — an injected parent agent could redirect the model call
    to a hostile endpoint and leak the key/context.

    ``user_content_kwargs`` merges content-block keys (e.g. ``cache_control`` for
    Anthropic prompt caching) onto the judge's last user message; a provider-unknown
    key surfaces as a loud provider error. On a checkpointed thread the model call keeps
    only the newest mark (older marks are stripped), so per-turn marking stays within the
    provider's per-request breakpoint cap. The
    judge/voter system messages are internal fixed prompts, so ``system_content_kwargs``
    has no seat and is rejected.

    ``extra="forbid"`` rejects any unknown key loudly at validation rather than
    letting a typo at the run door vanish silently.
    """

    model_config = ConfigDict(extra="forbid")

    judge_tools: list[str] | None = None
    voter_tools: list[str] | None = None
    judge_message: TemplatedText | None = None
    voter_message: TemplatedText | None = None
    judge_llm_provider: str | None = None
    checkpoint_provider: str | None = None
    judge_llm_kwargs: dict[str, Any] | None = None
    voters: list[VoterSpec] | None = None
    judge_langgraph_config: dict[str, Any] | None = None
    voter_langgraph_config: dict[str, Any] | None = None
    user_content_kwargs: dict[str, Any] | None = None

    @field_validator("user_content_kwargs")
    @classmethod
    def _empty_content_kwargs_is_unset(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """An empty dict carries no content-block keys — normalize {} to None so it reads as unset.

        Matches the builders that treat {} as no mark.
        """
        return value or None


@tai42_app.agents.agent("voting_agent", tags={"agents"})
class VotingAgent(Agent):
    """Run a voting workflow: voter LLMs answer in parallel, then a judge LLM decides.

    ``run`` returns the ``VotingOutput``; ``astream`` streams the judge's steps and ends with a
    ``StructuredFinal`` of the same ``VotingOutput``.
    """

    tool_name: ClassVar[str] = "voting_agent"
    tool_description: ClassVar[str] = (
        "Run a voting workflow: multiple voter LLM agents in parallel, then a judge "
        "LLM agent evaluates their responses."
    )
    ToolInput: ClassVar[type[BaseModel]] = VotingAgentInput

    @classmethod
    def addressed_memory(cls, run_kwargs: Mapping[str, Any]) -> AddressedMemory:
        """The checkpoint provider when present, and the ids the judge and voter configs carry."""
        threads: set[str] = set()
        checkpoints: set[str] = set()
        for key in ("judge_langgraph_config", "voter_langgraph_config"):
            role_threads, role_checkpoints = config_addressed(run_kwargs.get(key))
            threads |= role_threads
            checkpoints |= role_checkpoints
        return AddressedMemory(
            keys=frozenset(_CONTINUITY_KEYS & set(run_kwargs)),
            thread_ids=frozenset(threads),
            checkpoint_ids=frozenset(checkpoints),
        )

    async def run(self, **kwargs: Any) -> VotingOutput:
        """Drain ``astream`` to its terminal ``VotingOutput``.

        ``astream``'s guard refuses an undeclared keyword and a contract key this agent cannot
        honor. The drain forces the ``VotingOutput`` structured terminal — a missing one raises
        rather than falling back to message text.
        """
        return await self._drain(self.astream(**kwargs), response_format=VotingOutput)

    async def astream(
        self,
        *,
        judge_tools: list[str] | None = None,
        voter_tools: list[str] | None = None,
        judge_message: TemplatedText | None = None,
        voter_message: TemplatedText | None = None,
        judge_llm_provider: str | None = None,
        checkpoint_provider: str | None = None,
        judge_llm_kwargs: dict[str, Any] | None = None,
        voters: list[VoterSpec] | None = None,
        judge_langgraph_config: dict[str, Any] | None = None,
        voter_langgraph_config: dict[str, Any] | None = None,
        user_content_kwargs: dict[str, Any] | None = None,
        response_format: Any = None,
        thread_id: str | None = None,
        user_message: TemplatedText | None = None,
        tool_names: Sequence[str] = (),
        **unknown: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Resolve the judge/voter tool names, run the voters, stream the judge, and yield the terminal.

        The terminal is a ``StructuredFinal`` carrying the full ``VotingOutput``.

        ``user_content_kwargs`` merges content-block keys (e.g. ``cache_control``)
        onto the judge's last user message (the final voter verdict when voters are
        present); a provider-unknown key surfaces as a loud provider error. A ``response_format``
        other than ``VotingOutput``, a set ``thread_id`` / ``user_message`` / ``tool_names``, or a
        keyword this face does not declare is refused loudly.
        """
        guard_run_kwargs(
            "voting_agent.astream",
            unknown,
            {
                "thread_id": thread_id,
                "user_message": user_message,
                "tool_names": tool_names,
                "response_format": None if response_format is VotingOutput else response_format,
            },
            _UNHONORED_REASONS,
        )
        judge_llm_provider = judge_llm_provider or llm_provider_settings().llm
        judge_llm_kwargs = judge_llm_kwargs or {}
        judge_graph = await tools_agent_graph(
            ToolsAgentGraphSpec(
                tool_names=tuple(judge_tools or ()),
                system_message=JUDGE_SYSTEM_MESSAGE,
                llm_provider=judge_llm_provider,
                llm_kwargs=judge_llm_kwargs,
                checkpoint_provider=checkpoint_provider,
            )
        )

        # One trace per voting run: resolve the run's lineage once (the judge config is the
        # lead role's carrier), then derive the voter and judge configs from it so every
        # voter and the judge nest under the ONE run trace rather than each minting a fresh
        # root. Each voter still gets its own thread_id and callbacks.
        run_context = resolve_trace_context(judge_langgraph_config)
        voter_config = with_run_trace_lineage(voter_langgraph_config, run_context)
        judge_config = with_run_trace_lineage(judge_langgraph_config, run_context)

        user_messages, voters_info = await _run_voters(
            judge_message,
            voter_message,
            judge_llm_provider,
            judge_llm_kwargs,
            voters,
            voter_tools,
            checkpoint_provider,
            voter_config,
        )

        judge_verdict = ""
        judge_model: str | None = None
        async for event in astream_tools_agent_events(
            graph=judge_graph, user_message=user_messages, config=judge_config, user_content_kwargs=user_content_kwargs
        ):
            if isinstance(event, MessageFinal):
                judge_verdict = event.text
            elif isinstance(event, RunUsage):
                judge_model = event.model
            yield event

        yield StructuredFinal(
            data=VotingOutput(
                judge=VoteInfo(
                    provider=judge_llm_provider,
                    model=judge_model or judge_llm_kwargs.get("model"),
                    verdict=judge_verdict,
                ),
                voters=voters_info,
            )
        )
