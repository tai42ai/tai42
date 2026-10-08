"""The deep agent's run thread: its run config, its place in the finished-thread ledger, its stream end."""

from __future__ import annotations

from typing import Any

from tai42_agents._internal.config_util import (
    build_run_config,
    init_langgraph_config,
    mark_minted_thread_finished,
    start_run_thread,
)


async def start_run_config(
    langgraph_config: dict[str, Any] | None,
    thread_id: str | None,
    resume_checkpoint_id: str | None,
    recursion_limit: int | None,
    checkpoint_provider: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Build the run config both faces run the graph with, and ready its thread in the ledger.

    The caller's ``langgraph_config`` is the read-only base; ``thread_id`` /
    ``resume_checkpoint_id`` overlay its ``configurable`` and ``recursion_limit`` overlays the
    top level. With no thread pinned, :func:`init_langgraph_config` mints a fresh isolated one.
    ``recursion_limit`` bounds the TOP-LEVEL graph ONLY: each task-tool subagent runs its own
    graph bound by deepagents at 9999, so the effective step budget is MULTIPLICATIVE across
    nesting depth, not a total-spend ceiling.

    Returns ``(config, minted_thread)``: a caller-supplied thread leaves the ledger before the
    run; a thread minted for a keyless run is returned so the face marks it finished when the
    run ends without a park.
    """
    source = build_run_config(langgraph_config, thread_id, resume_checkpoint_id, recursion_limit)
    config = init_langgraph_config(config=source).config
    return config, await start_run_thread(source, config, provider=checkpoint_provider)


async def end_streamed_run(
    *,
    response_format: Any,
    saw_structured: bool,
    paused: bool,
    minted_thread: str | None,
    checkpoint_provider: str | None,
) -> None:
    """Close a drained stream: a run that paused ends nothing; any other run is at its terminal.

    A requested ``response_format`` that produced no terminal answer fails loudly (a pending
    interrupt or an async park skips the raise, as in ``_drain``); a run that reached its
    terminal marks the thread it minted finished.
    """
    if paused:
        return
    if response_format is not None and not saw_structured:
        raise RuntimeError("agent run requested a response_format but produced no structured output")
    await mark_minted_thread_finished(minted_thread, provider=checkpoint_provider)
