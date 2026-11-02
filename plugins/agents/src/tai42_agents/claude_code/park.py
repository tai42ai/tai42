"""Parking a ``claude_code`` turn on an interaction.

A turn parks two ways: the agent's OWN async ask, and a proxied tool call that async-parked.
Both build the same park identity, record the parked interaction into the durable index, stop
the runner and end the turn on a ``SuspendedFinal``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tai42_contract.agent.events import SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.interactions import SuspendedInteraction

from tai42_agents._internal.park import ParkIdentity, assert_park_capable, chain_routing_slots, persist_park
from tai42_agents.claude_code.protocol import AskFrame, StopFrame, dump_frame
from tai42_agents.claude_code.settings import ClaudeCodeSettings


def session_park_identity(
    *, agent_name: str, thread_id: str, settings: ClaudeCodeSettings, options_snapshot: dict[str, Any]
) -> tuple[ParkIdentity, datetime]:
    """The park identity of a threaded session and its retention horizon, gated for a durable park.

    The horizon is the session's TTL from now; ``assert_park_capable`` raises when the park
    could not be resumed within it.
    """
    horizon = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    completion_tool, completion_context = chain_routing_slots()
    identity = ParkIdentity(
        agent_name=agent_name,
        thread_id=thread_id,
        rebuild_kwargs={"thread_id": thread_id, "options_snapshot": options_snapshot},
        bind=True,
        completion_tool=completion_tool,
        completion_context=completion_context,
        retention_bound=horizon,
    )
    assert_park_capable(identity, durable=True, retention_bound=horizon)
    return identity, horizon


async def park_async_ask(
    frame: AskFrame, *, identity: ParkIdentity, handle: Any, thread_id: str, horizon: datetime
) -> SuspendedFinal:
    """Raise the agent's own async ask as a parked interaction and park the turn on it."""
    suspended = await tai42_app.interactions.ask(
        frame.question,
        answer_format=frame.answer_format,
        options=frame.options,
        schema=frame.form_schema,
        data=frame.data,
        pages=frame.pages,
        reaction_tool=frame.reaction_tool,
        reactions=frame.reactions,
        wording=frame.wording,
        mode="async",
        expiry_at=horizon,
    )
    if not (isinstance(suspended, SuspendedInteraction)):
        raise AssertionError  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    return await park_on_interaction(suspended, identity=identity, handle=handle, thread_id=thread_id, horizon=horizon)


async def park_on_interaction(
    suspended: SuspendedInteraction,
    *,
    identity: ParkIdentity,
    handle: Any,
    thread_id: str,
    horizon: datetime,
) -> SuspendedFinal:
    """Record an already-created parked interaction into the durable index, stop the runner, and suspend.

    The shared park tail BOTH the agent's OWN async ask and a tool the agent drives that
    async-parks cross into the index through — each supplies its interaction, this persists +
    stops + suspends uniformly.

    ``horizon`` is this session's retention bound; the interaction's own deadline (bounded
    by the retention gate) is what the entry is keyed to, falling back to the horizon when
    the sentinel carried none.
    """
    interaction_id = suspended.interaction_id
    deadline = (suspended.expiry_at or horizon).isoformat()
    # interrupt_id == interaction_id (a one-ask park); persist BEFORE the stop/drain so an
    # instant human answer never waits on the reaper.
    await persist_park(identity, [(interaction_id, {interaction_id: deadline})])
    await handle.write_stdin(dump_frame(StopFrame(reason="park")))
    return SuspendedFinal(
        interaction_ids=[interaction_id],
        thread_id=thread_id,
        # The caller subset the parked sentinel carried (``[id]`` for a ``to="caller"`` ask,
        # empty for a user ask), so the receipt partitions caller from user asks.
        caller_interaction_ids=suspended.caller_interaction_ids,
        expiry_at=deadline,
    )
