"""Gate probes for the deferred-call legs: a tool a run calls after the reply.

Each records ``{key, idempotency_key, pid}`` on ``e2e:rec:{key}`` — the idempotency key the
platform hands a deferred call in its tool extras, and the process that ran it — then blocks
on the Redis list ``e2e:gate:{key}`` until the harness pushes onto it (a 30 s ceiling, so a
mis-scripted test cannot hang the worker). A test releases the gate to let the call finish,
or kills the worker while the call is blocked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable
from typing import cast

from tai42_contract.app import tai42_app
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings

# The longest a gate waits for its release before the call fails.
_GATE_CEILING_SECONDS = 30


async def _record_and_wait(key: str) -> str:
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    idempotency_key = tai42_app.tools.extras().get(TOOL_EXTRA_IDEMPOTENCY_KEY)
    record = json.dumps({"key": key, "idempotency_key": idempotency_key, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast("Awaitable[int]", client.rpush(f"e2e:rec:{key}", record))
        released = await cast(
            "Awaitable[list[bytes] | None]", client.blpop([f"e2e:gate:{key}"], timeout=_GATE_CEILING_SECONDS)
        )
    if released is None:
        raise TimeoutError(f"e2e gate {key!r} was not released within {_GATE_CEILING_SECONDS}s")
    return "released"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_deferred_gate(key: str) -> str:
    """Record ``{key, idempotency_key, pid}`` on ``e2e:rec:{key}``, then block until ``e2e:gate:{key}`` is pushed."""
    return await _record_and_wait(key)


@tai42_app.tools.tool(tags={"e2e"}, meta={"tai42/crash_resume": True})
async def e2e_deferred_gate_resumable(key: str) -> str:
    """The same gate, declared crash-resumable: a call a process exit interrupted is run again."""
    return await _record_and_wait(key)


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_defer_after_reply(target: str) -> dict[str, str]:
    """Defer ``target`` with no arguments in a states unit; return the live ``{"trace_id", "span_id"}``.

    The unit stages nothing but the call and commits, so the call runs after the reply, in this
    call's trace under the span live here.
    """
    writer = tai42_app.monitoring.active.writer
    trace_id = writer.current_trace_id()
    span_id = writer.current_span_id()
    if trace_id is None or span_id is None:
        raise RuntimeError("the defer probe ran with no live recorded span")
    async with tai42_app.states.open_unit() as unit:
        await unit.defer_call(target, {})
        await unit.commit()
    return {"trace_id": trace_id, "span_id": span_id}
