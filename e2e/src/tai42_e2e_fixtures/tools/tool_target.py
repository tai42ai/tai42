"""The tool-target async-park leg riding the conversation door's own park-completion
binding: the parking tool-target and its stored resume continuation that delivers the
deferred outcome back into the conversation. Registers at import via ``@tai42_app.tools.tool``."""

from __future__ import annotations

import json
import os

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings

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


def _tool_target_binding_key(interaction_id: str) -> str:
    """The probe-store key the parking tool stashes the door's park-completion binding under,
    so the out-of-band resumer (which is handed only ``{interaction_id, answer}``) can recover
    the opaque delivery context to fire the completion with."""
    return f"e2e:tool_target_binding:{interaction_id}"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_tool_target_park(marker: str) -> object:
    """A conversation tool-target that async-parks on ``ask_user`` and later delivers its
    resumed reply back through the door's generic park-completion binding.

    Runs as the target of a ``target_kind=tool`` conversation route. It READS the completion
    binding the conversation door bound around this dispatch (``get_park_completion`` — the
    ``deliver_tool_completion`` tool name plus the opaque ``{delivery_thread_id, route_name}``
    context the door pins), binds its OWN resume continuation (``e2e_tool_target_deliver``) so
    ``ask_user(mode="async")`` may park, and parks — returning the ``SuspendedInteraction`` the
    tool seam keeps by type, so the turn ends SILENTLY (no synchronous reply). The turn already
    runs under the route's bound execution identity, so the park stores THAT as its
    continuation identity and the resume rebinds as it — the tool binds no identity of its own.

    It stashes the captured park-completion binding under the parked interaction id (so the
    out-of-band resumer can recover the opaque context) and RPUSHes the parked interaction id
    onto ``e2e:rec:tool_target_park:{marker}`` (so a spec that minted ``marker`` reads back which
    interaction this turn parked on, and that the turn ran to the park at all — the silent turn's
    completion barrier)."""
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_contract.interactions import (
        get_park_completion,
        reset_resume_continuation_tool,
        set_resume_continuation_tool,
    )
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.interactions import ask_user

    # The door binds ``deliver_tool_completion`` + its opaque delivery context around this
    # dispatch; capture it so the out-of-band resumer can fire it. A binding of ``(None, None)``
    # means the door wired none — fail loud rather than park with no path back.
    completion_tool, completion_context = get_park_completion()
    if completion_tool is None or completion_context is None:
        raise RuntimeError("e2e_tool_target_park ran with no park-completion binding from the conversation door")

    tool_token = set_resume_continuation_tool("e2e_tool_target_deliver")
    try:
        expiry_at = datetime.now(UTC) + timedelta(seconds=_TOOL_TARGET_PARK_EXPIRY_SECONDS)
        suspended = await ask_user(marker, mode="async", expiry_at=expiry_at)
    finally:
        reset_resume_continuation_tool(tool_token)

    binding = json.dumps({"tool": completion_tool, "context": dict(completion_context)})
    park_record = json.dumps({"interaction_id": suspended.interaction_id, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        # Outlive the answer round trip generously; the far park deadline caps the leg anyway.
        await cast(
            "Awaitable[bool | None]", client.set(_tool_target_binding_key(suspended.interaction_id), binding, ex=600)
        )
        await cast(Awaitable[int], client.rpush(f"e2e:rec:tool_target_park:{marker}", park_record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_tool_target_deliver(interaction_id: str, answer: object) -> dict:
    """The stored resume continuation the tool-target park fires ONCE out of band.

    The platform's continuation seam fires it with ``{interaction_id, answer}`` under the park's
    stored (route execution-key) identity, on whichever worker resolves the park. It recovers the
    park-completion binding ``e2e_tool_target_park`` stashed under the interaction id and drives
    the REAL ``deliver_tool_completion`` — imported and called directly, exactly as the platform's
    own resumer does (a platform-internal delivery, never a scope-gated model tool dispatch), the
    same way the sibling ``e2e_async_resume`` probe performs its platform resume work directly:

    * a genuine answer is a clean-success terminal — the outcome ``{"result": {"answer": answer}}``
      is delivered ``succeeded`` and mapped through the ORIGINATING route's ``reply_expr``;
    * the generic expiry marker OR the abort sentinel is a NON-SUCCESS terminal — delivered
      ``failed``, so the route's uniform client-safe notice is delivered (never a mapped reply,
      never silence).

    ``completion_id`` is the parked interaction id — the stable idempotency id of this terminal,
    so an at-least-once redelivery re-fires with the SAME id and ``deliver_tool_completion``
    no-ops on the already-committed record. RPUSHes ``{status, pid}`` onto
    ``e2e:rec:tool_target_deliver:{interaction_id}`` so a spec reads that the resume fired and
    through which terminal."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.interactions import EXPIRY_ANSWER, PARK_COMPLETION_FAILED, PARK_COMPLETION_SUCCEEDED
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.conversations.turn import deliver_tool_completion

    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        raw_binding = await cast("Awaitable[str | None]", client.get(_tool_target_binding_key(interaction_id)))
    if raw_binding is None:
        raise RuntimeError(f"tool-target resume for interaction {interaction_id!r} has no stashed park binding")
    context = json.loads(raw_binding)["context"]

    non_success = answer in (EXPIRY_ANSWER, _TOOL_TARGET_ABORT_ANSWER)
    if non_success:
        status = PARK_COMPLETION_FAILED
        result: object = {"reason": "aborted"}
    else:
        status = PARK_COMPLETION_SUCCEEDED
        # Shaped so the route's ``reply_expr`` (``.result.answer``) maps the resumed answer.
        result = {"result": {"answer": answer}}

    await deliver_tool_completion(
        delivery_thread_id=context["delivery_thread_id"],
        completion_id=interaction_id,
        result=result,
        status=status,
        route_name=context.get("route_name"),
    )
    record = json.dumps({"status": status, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:tool_target_deliver:{interaction_id}", record))
    return {"status": status}
