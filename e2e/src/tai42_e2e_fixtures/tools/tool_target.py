"""The tool-target async-park leg riding the conversation door's own park-completion
binding: the parking tool-target and its stored resume continuation that delivers the
deferred outcome back into the conversation. Registers at import via ``@tai42_app.tools.tool``."""

from __future__ import annotations

import json
import os

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as

# The tool-target async-park leg (the unified park-completion binding). Unlike the flow-driver
# and agent probes above, THIS pair rides the CONVERSATION door's OWN park-completion binding:
# ``_run_tool_turn`` binds the generic ``deliver_tool_completion`` continuation (carrying the
# turn's thread as the opaque delivery address AND the originating route name) around a tool
# dispatch, so a tool that async-parks captures that binding and its resumer fires it to deliver
# the deferred outcome back into the conversation. ``e2e_tool_target_park`` is the parking
# tool-target; ``e2e_tool_target_deliver`` is its stored resume continuation, fired once out of
# band on whichever worker resolves the park. Generic: no flow/engine state, only the opaque
# park-completion binding the door produced and the parked interaction id.

# Far enough out that a driven answer always resolves the park first — the reaper never races
# these legs (they resolve by answer or by an aborted-resume terminal, never by expiry).
_TOOL_TARGET_PARK_EXPIRY_SECONDS = 3600.0

# A sentinel answer the resume continuation reads as a NON-SUCCESS terminal (an aborted/failed
# resume): it drives ``deliver_tool_completion`` with the FAILED status so the uniform
# client-safe notice is delivered — the non-success arm, without waiting on the expiry reaper.
_TOOL_TARGET_ABORT_ANSWER = "__abort__"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_tool_target_park(marker: str) -> object:
    """A conversation tool-target that async-parks on ``ask`` and later delivers its
    resumed reply back through the door's generic park-completion binding.

    Runs as the target of a ``target_kind=tool`` conversation route. The conversation door binds
    ``deliver_tool_completion`` plus its opaque ``{delivery_thread_id, route_name}`` context around
    this dispatch, so ``ask(mode="async")`` captures that binding as the park's stored delivery
    address; this tool binds its OWN resume continuation (``e2e_tool_target_deliver``) and parks —
    returning the ``SuspendedInteraction`` the tool seam keeps by type, so the turn ends SILENTLY
    (no synchronous reply). The turn already runs under the route's bound execution identity, so
    the park stores THAT as its continuation identity and the resume rebinds as it — the tool binds
    no identity of its own.

    It verifies the door wired the binding (fail loud rather than park with no path back) and
    RPUSHes the parked interaction id onto ``e2e:rec:tool_target_park:{marker}`` (so a spec that
    minted ``marker`` reads back which interaction this turn parked on, and that the turn ran to
    the park at all — the silent turn's completion barrier)."""
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_contract.interactions import get_park_completion
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.interactions import ask

    # The door binds ``deliver_tool_completion`` + its opaque delivery context around this
    # dispatch; ``ask`` captures it as the park's stored delivery address. A binding of
    # ``(None, None)`` means the door wired none — fail loud rather than park with no path back.
    completion_tool, completion_context = get_park_completion()
    if completion_tool is None or completion_context is None:
        raise RuntimeError("e2e_tool_target_park ran with no park-completion binding from the conversation door")

    with driving_as(continuation="e2e_tool_target_deliver"):
        expiry_at = datetime.now(UTC) + timedelta(seconds=_TOOL_TARGET_PARK_EXPIRY_SECONDS)
        suspended = await ask(marker, mode="async", expiry_at=expiry_at)

    park_record = json.dumps({"interaction_id": suspended.interaction_id, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:tool_target_park:{marker}", park_record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_tool_target_deliver(interaction_id: str, answer: object) -> dict:
    """The stored resume continuation the tool-target park fires ONCE out of band.

    The platform's continuation seam fires it with ``{interaction_id, answer}`` under the park's
    stored (route execution-key) identity, on whichever worker resolves the park. It RETURNS the
    resumed run's TERMINAL; the platform's delivery ladder then fires the door's bound
    ``deliver_tool_completion`` under its own delivery-fire context and maps the terminal through
    the ORIGINATING route's ``reply_expr`` — an address tool delivers ONLY inside that fire, never
    from a raw resumer call:

    * a genuine answer is a clean-success terminal — ``{"result": {"answer": answer}}`` carries no
      failure status, so the ladder delivers it ``succeeded`` mapped through ``reply_expr``;
    * the generic expiry marker OR the abort sentinel is a NON-SUCCESS terminal — a mapping stamped
      with a failure ``status`` — so the ladder delivers ``failed`` and the route's uniform
      client-safe notice is sent (never a mapped reply, never silence).

    RPUSHes ``{status, pid}`` onto ``e2e:rec:tool_target_deliver:{interaction_id}`` so a spec reads
    that the resume fired and through which terminal; the platform keys delivery on the run's
    completion id, so a redelivery of the same terminal lands once."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.interactions import EXPIRY_ANSWER, PARK_COMPLETION_FAILED, PARK_COMPLETION_SUCCEEDED
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    non_success = answer in (EXPIRY_ANSWER, _TOOL_TARGET_ABORT_ANSWER)
    if non_success:
        status = PARK_COMPLETION_FAILED
        # A failure-stamped terminal — the delivery ladder reads its status and delivers the
        # route's uniform notice, never this body.
        outcome: dict[str, object] = {"status": PARK_COMPLETION_FAILED, "result": {"reason": "aborted"}}
    else:
        status = PARK_COMPLETION_SUCCEEDED
        # No failure status, shaped so the route's ``reply_expr`` (``.result.answer``) maps it.
        outcome = {"result": {"answer": answer}}

    record = json.dumps({"status": status, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:tool_target_deliver:{interaction_id}", record))
    return outcome
