"""Conversation-bridge lifecycle hooks, registered by the app at construction whatever routers it mounts."""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.conversations.cache import get_conversations_manager


async def register_conversation_completion_tools() -> None:
    """Force-register the two hidden completion-delivery tools.

    The continuations a parked turn's resumed outcome is delivered through: ``conversation_deliver``
    for a parked AGENT turn (the agent's own final answer) and ``deliver_tool_completion`` for a
    parked TOOL turn (the terminal outcome mapped through the route's ``reply_expr``).

    Both are mandatory bridge mechanisms (never operator-excludable catalog tools), so each is
    registered ``force=True`` and ``tai42/hidden`` — never offered to a model, reached only
    when a resumed run's resumer fires it. Registered in every epoch build, so the completion
    continuation the turn engine binds always resolves.
    """
    from tai42_skeleton.conversations.turn import (
        COMPLETION_TOOL_NAME,
        DELIVER_TOOL_COMPLETION_NAME,
        deliver_agent_completion,
        deliver_tool_completion,
    )

    tai42_app.tools.tool(
        deliver_agent_completion,
        name=COMPLETION_TOOL_NAME,
        tags={"conversations"},
        meta={"tai42/hidden": True},
        force=True,
    )
    tai42_app.tools.tool(
        deliver_tool_completion,
        name=DELIVER_TOOL_COMPLETION_NAME,
        tags={"conversations"},
        meta={"tai42/hidden": True},
        force=True,
    )


async def redrive_pending_conversations() -> None:
    """Resume every unfinished conversation record on boot, so nothing is stranded across a restart.

    Intake re-drive must run FIRST: it gives every stranded ``accepted`` record a terminal
    outcome, which is work the delivery re-drive then picks up. No-op with no backend. The
    periodic sweep is established separately (post-swap), so it survives a reload rather
    than being spawned on the throwaway build-thread loop this handler runs on at reload.
    """
    if not get_conversations_manager().durable:
        return
    from tai42_skeleton.conversations import redrive_accepted, redrive_pending

    await redrive_accepted()
    await redrive_pending()


def start_conversations_delivery_sweep() -> None:
    """(Re)establish the periodic stalled-delivery sweep on the serving loop.

    Run at boot and after every epoch swap, both ON the serving loop, so the sweep task
    attaches to the loop its deliveries run on and retires with its generation. The sweep
    is what recovers a record whose worker died holding a still-live lease. No-op with no
    backend.
    """
    if not get_conversations_manager().durable:
        return
    from tai42_skeleton.conversations import start_delivery_sweep

    start_delivery_sweep()


async def stop_conversations_delivery_sweep() -> None:
    """Cancel and await the stalled-delivery sweep on the serving loop it lives on.

    A backend-less deployment never started one.
    """
    if not get_conversations_manager().durable:
        return
    from tai42_skeleton.conversations import stop_delivery_sweep

    await stop_delivery_sweep()
