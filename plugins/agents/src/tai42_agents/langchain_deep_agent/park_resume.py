"""The resumed drive of a parked ``langchain_deep_agent`` run.

:meth:`DeepAgent.aresume_park <tai42_agents.langchain_deep_agent.agent.DeepAgent.aresume_park>`
opens the run's trace root and hands the drive to :func:`drive_park_resume`: it reattaches the
park's durable workspace under the shared workspace lease, rebuilds the parked graph from the
stored identity, checks every resumed interrupt is still pending (or re-produces the terminal of a
run that already finished), re-binds the park so a re-park re-persists a fresh index, and drains
one ``Command(resume=...)`` through the agent's shared streaming core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.types import Command
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import TraceContext
from tai42_kit.llm.runtime import build_user_output, extract_structured_output

from tai42_agents._internal.config_util import thread_head_config
from tai42_agents._internal.lc_tools import structured_tools
from tai42_agents._internal.park import (
    build_park_identity,
    chain_routing_slots,
    collect_pending_interrupts,
    is_suspended_receipt,
    park_drive,
    require_stored_checkpoint,
)
from tai42_agents._internal.park.errors import AgentResumeInterruptNotPendingError
from tai42_agents.langchain_deep_agent.run_input import DeepAgentInput
from tai42_agents.langchain_deep_agent.run_thread import start_run_config
from tai42_agents.langchain_deep_agent.session import DeepAgentSession
from tai42_agents.langchain_deep_agent.spec import InlineSkill
from tai42_agents.langchain_deep_agent.tool_spec import resolve_subagent_specs

if TYPE_CHECKING:
    from tai42_agents.langchain_deep_agent.agent import DeepAgent


async def drive_park_resume(
    deep_agent: DeepAgent,
    context: TraceContext | None,
    *,
    validated: DeepAgentInput,
    rebuild_kwargs: dict[str, Any],
    recursion_limit: int | None,
    workspace_key: str | None,
    thread_id: str,
    resume_map: dict[str, dict[str, Any]],
) -> Any:
    """Drive one :meth:`DeepAgent.aresume_park` of ``deep_agent`` inside its lineage ``context``."""
    response_format = validated.response_format
    interrupt_on = validated.interrupt_on

    client_tools = structured_tools(
        await tai42_app.tools.get_client_tools(list(validated.tool_names)) if validated.tool_names else []
    )
    internal_subagents = await resolve_subagent_specs(validated.subagents)
    coerced_inline_skills = [
        s if isinstance(s, InlineSkill) else InlineSkill(**s) for s in (validated.inline_skills or [])
    ]

    # Reattach the SAME durable volume by workspace_key and serialize this drive on the shared
    # workspace lease, exactly like a fresh turn: the lease wraps session-acquire +
    # cred-materialize + drive, so a concurrent resume for the same thread loses the lease
    # before opening a second session on the volume.
    async with DeepAgentSession.leased(thread_id=thread_id, workspace_key=workspace_key) as drive:
        suspended = False
        try:
            agent, strategy = await deep_agent._resolve_and_build(
                tools=client_tools,
                subagents=internal_subagents,
                skills=validated.skills,
                inline_skills=coerced_inline_skills or None,
                system_message=(validated.system_message.content or "") if validated.system_message else "",
                response_format=response_format,
                interrupt_on=interrupt_on,
                llm_provider=validated.llm_provider,
                checkpoint_provider=validated.checkpoint_provider,
                store_provider=validated.store_provider,
                llm_kwargs=validated.llm_kwargs,
                session=drive.session,
            )
            config, _minted = await start_run_config(
                validated.langgraph_config,
                thread_id,
                None,
                recursion_limit,
                validated.checkpoint_provider,
                context,
            )
            # The parked drive wrote its checkpoints after any checkpoint its run was pinned to,
            # so the resume continues from the thread's latest checkpoint.
            config = thread_head_config(config)

            snapshot = await agent.aget_state(config, subgraphs=True)
            require_stored_checkpoint(snapshot, thread_id)
            pending_ids = {iid for iid, _ in collect_pending_interrupts(snapshot)}
            missing = [interrupt_id for interrupt_id in resume_map if interrupt_id not in pending_ids]
            if missing:
                if pending_ids:
                    # The graph is still parked, but not on every interrupt this super-step
                    # resumes — a corrupted routing, or a graph that advanced past one. Raise.
                    raise AgentResumeInterruptNotPendingError(next(iter(resume_map[missing[0]])), missing[0])
                # A stored checkpoint with no interrupt pending: the super-step already drove to a
                # CLEAN TERMINAL, so this is an idempotent re-drive after a crash between the terminal
                # drive and the index finalize. Re-produce the SAME terminal output from the persisted
                # state — never re-invoke a resolved graph — so the completion handoff re-fires
                # under its stable id and the delivery ledger dedupes it to one delivery.
                if response_format is not None:
                    return extract_structured_output(snapshot.values, response_format)
                return build_user_output(snapshot.values)

            # Bind again so a re-park on resume re-persists a fresh index (resume re-enters
            # through the agent's own bound entrypoint); the completion tool the driver
            # rebound is captured onto the new entry. No live tools on a rebuilt graph, so it is
            # park-capable by construction. The retention bound is min(checkpoint, workspace).
            completion_tool, completion_context = chain_routing_slots()
            park = build_park_identity(
                agent_name=deep_agent.tool_name,
                config=config,
                checkpoint_provider=validated.checkpoint_provider,
                has_live_tools=False,
                rebuild_kwargs=rebuild_kwargs,
                recursion_limit=recursion_limit,
                completion_tool=completion_tool,
                completion_context=completion_context,
                bind=True,
                extra_retention_horizon=drive.workspace_retention_horizon,
            )
            async with park_drive(park):
                result = await deep_agent._drain(
                    deep_agent._astream_built(
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
            suspended = is_suspended_receipt(result)
            return result
        finally:
            if not suspended:
                await drive.scrub_credentials()
