"""The proxied tool-call handler for ``claude_code``.

Run one tool the runner requested, write its result back, and surface (or loudly
refuse) an async park.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    NestedParkOwnershipError,
    SuspendedInteraction,
    assert_park_adoptable,
)

from tai42_agents._internal.nested_dispatch import nested_tool_dispatch
from tai42_agents.claude_code.protocol import ProtocolError, ToolCallFrame, ToolResultFrame, dump_frame


async def run_proxied_tool_call(
    frame: ToolCallFrame, *, handle: Any, allowlist: set[str], thread_id: str | None
) -> SuspendedInteraction | None:
    """Run one proxied tool call and write its result back to the runner.

    Returns the park sentinel when the tool async-parked (so the drive loop stops
    the runner and suspends), else ``None``.

    A tool that returns a :class:`SuspendedInteraction` async-parked its caller (a generic
    contract sentinel — this loop learns nothing of the tool's resume machinery). On a
    threaded run it is surfaced UP as a park, exactly as the agent's own async ask is; on a
    thread-less (ephemeral) run it can never be resumed, so it is refused loudly to the
    model as a tool error, mirroring the ephemeral async-ask refusal — never a silent
    unresumable park.

    A park this run does not OWN is refused the same way: a tool that drove a nested run
    (a flow, another agent) surfaces a park resumed on THAT run's path, so parking this
    session on it would suspend the session with nothing to resume it
    (:func:`assert_park_adoptable`). This seam claims a park from the SENTINEL only — it
    never reads a wire-form marker off a tool result — so the check here is the whole
    claim check for this agent; there is no second, content-shaped path into its index.

    The dispatch runs delivery-scoped and UNCHAINED: THIS agent owns the interaction and its
    deferred answer, so a parking driver reached through the tool must not capture the
    completion binding addressing that answer (see
    :mod:`~tai42_agents._internal.nested_dispatch`). It is not chained because this session
    resumes by feeding the runner the answer to the interaction its pending tool call is
    waiting on — a park on the CALL is not a shape its protocol can resume — so a nested
    run's park is refused here rather than waited on. The park surfaced up here is therefore
    the agent's own — raised outside the scoped call, and the ownership check above is what
    keeps that true.
    """
    if frame.tool_name not in allowlist:
        # A compromised session cannot widen its declared tool set — a loud protocol error.
        raise ProtocolError(
            f"runner requested tool {frame.tool_name!r} outside the granted allowlist {sorted(allowlist)}"
        )
    try:
        with nested_tool_dispatch():
            result = await tai42_app.tools.run_tool(frame.tool_name, frame.arguments)
    except Exception as exc:
        await handle.write_stdin(dump_frame(ToolResultFrame(call_id=frame.call_id, result=str(exc), is_error=True)))
        return None
    if isinstance(result, SuspendedInteraction):
        # Ownership first: a park this session could never claim is refused for THAT
        # reason on a thread-less run too, rather than being reported as a thread-less
        # limitation the operator might try to fix by giving the run a thread.
        try:
            assert_park_adoptable(result.resume_owner, interaction_id=result.interaction_id, tool_name=frame.tool_name)
        except NestedParkOwnershipError as exc:
            await handle.write_stdin(dump_frame(ToolResultFrame(call_id=frame.call_id, result=str(exc), is_error=True)))
            return None
        if thread_id is None:
            await handle.write_stdin(
                dump_frame(
                    ToolResultFrame(
                        call_id=frame.call_id,
                        result="claude_code cannot async-park a tool-face (thread-less) run",
                        is_error=True,
                    )
                )
            )
            return None
        return result
    await handle.write_stdin(dump_frame(ToolResultFrame(call_id=frame.call_id, result=result)))
    return None
