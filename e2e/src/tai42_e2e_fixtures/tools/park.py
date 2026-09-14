"""Async-park probe tools: the flow-driver parks, the shared multi-park super-step
barrier, and the agent async-park leg. Each registers at import via
``@tai42_app.tools.tool`` and exposes an ``e2e_record`` Redis observable."""

from __future__ import annotations

import json
import os

from tai42_contract.app import tai42_app

from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings

# The synthetic execution identity the park driver binds before ``ask_user`` and the
# async park stores as its ``continuation_identity``. A value nothing else on the stack
# produces, so a continuation that fires under it proves the stored-identity rebind (the
# auth-off default execution identity is ``None``, never this).
_ASYNC_PARK_IDENTITY = "e2e-async-driver"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_park_flow(question: str, expiry_seconds: float) -> dict:
    """Drive an async ``ask_user`` park exactly as a resuming driver would.

    Binds ``e2e_async_resume`` as the current driver's resume continuation tool and a
    synthetic execution identity (``_ASYNC_PARK_IDENTITY``) to rebind that continuation as,
    then calls ``ask_user(mode="async", expiry_at=now+expiry_seconds)`` — which PARKS and
    returns a ``SuspendedInteraction`` at once, never blocking — and unbinds both. Returns
    the parked ``interaction_id`` and the STORED identity so a spec can answer (or await the
    expiry of) THIS park and assert the resume ran under that stored identity; a later answer
    or the expiry reaper fires ``e2e_async_resume`` as the stored identity, out of band, in
    whichever process resolves the park."""
    from datetime import UTC, datetime, timedelta

    from tai42_contract.interactions import reset_resume_continuation_tool, set_resume_continuation_tool
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask_user

    tool_token = set_resume_continuation_tool("e2e_async_resume")
    identity_token = set_execution_identity(
        CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-async-fp")
    )
    try:
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = await ask_user(question, mode="async", expiry_at=expiry_at)
    finally:
        reset_execution_identity(identity_token)
        reset_resume_continuation_tool(tool_token)
    assert suspended.expiry_at is not None  # async park always carries its deadline
    return {
        "interaction_id": suspended.interaction_id,
        "expiry_at": suspended.expiry_at.isoformat(),
        "pid": os.getpid(),
        "stored_identity": _ASYNC_PARK_IDENTITY,
    }


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_resume(interaction_id: str, answer: object) -> dict:
    """The generic resume continuation an async park fires ONCE — the stand-in flow
    driver's resume seam.

    Runs as the park's stored identity with ``{interaction_id, answer}``: the ANSWER
    branch records the delivered answer, the EXPIRY branch (``answer`` is the generic
    ``EXPIRY_ANSWER`` marker) records that the park expired unanswered. Reads the
    execution identity it is CURRENTLY bound under — the same seam the continuation runs
    beneath (``get_execution_identity().user_id``) — and RPUSHes
    ``{"branch", "value", "pid", "identity"}`` onto ``e2e:rec:async_resume:{interaction_id}``
    so the spec reads which branch this park resumed through, in which process, AND under
    which identity — proving the fire rebound the STORED park identity, not the answerer's
    or reaper's default (``None`` when the rebind is dropped)."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.interactions import EXPIRY_ANSWER
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    expired = answer == EXPIRY_ANSWER
    branch = "expiry" if expired else "answer"
    value = "expired" if expired else json.dumps(answer)
    bound_identity = get_execution_identity()
    identity = bound_identity.user_id if bound_identity is not None else None
    record = json.dumps({"branch": branch, "value": value, "pid": os.getpid(), "identity": identity})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:async_resume:{interaction_id}", record))
    return {"branch": branch}


# The N-park super-step barrier the multi-suspend leg drives. Three async parks are
# minted in ONE driver call (one logical super-step); each fires the shared
# ``e2e_async_multipark_resume`` continuation on whichever worker resolves it. The
# continuation receives only ``{interaction_id, answer}`` (flow-blind), so the driver
# routes each park's interaction id to its barrier slot on the probe channel.
_MULTIPARK_ROUTE_KEY = "e2e:multipark:route"


def _multipark_barrier_key(super_step_id: str) -> str:
    return f"e2e:multipark:barrier:{super_step_id}"


def _multipark_minted_key(super_step_id: str) -> str:
    return f"e2e:multipark:minted:{super_step_id}"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_multipark_flow(
    super_step_id: str, expiry_seconds: list[float], slow_slot: int = -1, slow_seconds: float = 0.0
) -> dict:
    """Drive N concurrent async ``ask_user`` parks in ONE super-step (one driver call).

    For each slot it binds ``e2e_async_multipark_resume`` as the resume continuation plus
    the synthetic park identity (``_ASYNC_PARK_IDENTITY``) and calls
    ``ask_user(mode="async", expiry_at=now+expiry_seconds[slot])`` — each PARKS and returns
    at once, so all N are suspended concurrently within this single call. It routes each
    park's ``interaction_id`` to its ``{super_step_id, slot, n, slow}`` on the probe channel
    so the flow-blind continuation can recover its barrier slot from the interaction id
    alone. ``slow_slot`` marks one slot whose continuation sleeps ``slow_seconds`` before
    applying, holding its due-record in flight past the reaper interval so a redelivery laps
    the original fire — the concurrent-redelivery hazard the barrier's idempotent apply must
    absorb. Returns the parked ``interaction_id`` per slot and the STORED identity."""
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_contract.interactions import reset_resume_continuation_tool, set_resume_continuation_tool
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask_user

    n = len(expiry_seconds)
    interaction_ids: list[str] = []
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        for slot, expiry in enumerate(expiry_seconds):
            tool_token = set_resume_continuation_tool("e2e_async_multipark_resume")
            identity_token = set_execution_identity(
                CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-async-fp")
            )
            try:
                expiry_at = datetime.now(UTC) + timedelta(seconds=expiry)
                suspended = await ask_user(f"{super_step_id}:{slot}", mode="async", expiry_at=expiry_at)
            finally:
                reset_execution_identity(identity_token)
                reset_resume_continuation_tool(tool_token)
            route = json.dumps(
                {
                    "super_step_id": super_step_id,
                    "slot": slot,
                    "n": n,
                    "slow": slot == slow_slot,
                    "slow_seconds": slow_seconds,
                }
            )
            await cast(Awaitable[int], client.hset(_MULTIPARK_ROUTE_KEY, suspended.interaction_id, route))
            interaction_ids.append(suspended.interaction_id)
    return {
        "super_step_id": super_step_id,
        "interaction_ids": interaction_ids,
        "stored_identity": _ASYNC_PARK_IDENTITY,
    }


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_multipark_resume(interaction_id: str, answer: object) -> dict:
    """The shared resume continuation for the multi-park super-step barrier.

    Recovers its ``{super_step_id, slot, n}`` from the interaction-id route, then applies
    its result to the barrier hash with HSETNX — idempotent, so an at-least-once redelivery
    (including one racing the original slow fire) fills its slot EXACTLY ONCE. It records the
    identity it is bound under (proving the STORED park-identity rebind, never the answerer's
    or reaper's default). The filler that completes all N slots wins a single ``SET ... NX``
    mint guard and RPUSHes ONE completion carrying every slot's result onto
    ``e2e:rec:multipark:{super_step_id}`` — so a duplicate fire can neither double-apply a
    slot nor double-mint the completion."""
    import asyncio
    from collections.abc import Awaitable
    from typing import cast

    from tai42_contract.interactions import EXPIRY_ANSWER
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        raw_route = await cast("Awaitable[str | None]", client.hget(_MULTIPARK_ROUTE_KEY, interaction_id))
        if raw_route is None:
            raise RuntimeError(f"multipark resume for unrouted interaction {interaction_id!r}")
        route = json.loads(raw_route)
        super_step_id = route["super_step_id"]
        slot = route["slot"]
        n = route["n"]
        if route["slow"]:
            # Hold this fire in flight past the reaper interval so a redelivery laps it,
            # exercising a concurrent redelivery the barrier's idempotent apply must absorb.
            await asyncio.sleep(route["slow_seconds"])  # noqa: TID251 — the concurrency hazard under test
        expired = answer == EXPIRY_ANSWER
        value = "expired" if expired else json.dumps(answer)
        bound_identity = get_execution_identity()
        identity = bound_identity.user_id if bound_identity is not None else None
        slot_payload = json.dumps({"value": value, "identity": identity})
        barrier_key = _multipark_barrier_key(super_step_id)
        await cast(Awaitable[int], client.hsetnx(barrier_key, str(slot), slot_payload))
        filled = await cast(Awaitable[int], client.hlen(barrier_key))
        if filled == n:
            minted = await cast(
                "Awaitable[bool | None]",
                client.set(_multipark_minted_key(super_step_id), str(os.getpid()), nx=True, ex=600),
            )
            if minted:
                fields = await cast("Awaitable[dict[str, str]]", client.hgetall(barrier_key))
                results = {field: json.loads(payload) for field, payload in fields.items()}
                completion = json.dumps({"n": n, "results": results, "pid": os.getpid()})
                await cast(Awaitable[int], client.rpush(f"e2e:rec:multipark:{super_step_id}", completion))
    return {"slot": slot}


# The AGENT async-park leg. Unlike the flow-driver probes above, the AGENT is the
# consumer: its own park machinery binds the ``agent_resume`` continuation around the
# drive, so these tools bind NO continuation. ``e2e_agent_async_ask`` supplies only the
# execution identity ``ask_user`` needs, and only when the stack has not already bound one
# (see its docstring), then returns the ``SuspendedInteraction`` the in-process tool seam
# stamps into the park marker the agent's park middleware interrupts on.
@tai42_app.tools.tool(tags={"e2e"})
async def e2e_agent_async_ask(question: str, expiry_seconds: float) -> object:
    """Async ``ask_user`` an agent tool call reaches, parking the agent run.

    INHERIT OR BIND. ``ask_user`` stores the CURRENTLY bound execution identity as the
    parked interaction's ``continuation_identity``, and the resolution door rebinds exactly
    that identity to fire the resume. So this probe binds its synthetic stand-in
    (:data:`_ASYNC_PARK_IDENTITY`) ONLY when nothing is bound — the auth-off stacks, which
    carry no caller identity and where ``ask_user`` would otherwise refuse the ask. Where the
    stack HAS bound one (an access-controlled profile running the turn as a conversation
    route's execution key), that real key is inherited untouched: it is the only identity
    that still carries authority at resume time, and overriding it with a policy-less
    synthetic would make the rebind refuse and the resume never run.

    RPUSHes ``{pid}`` onto ``e2e:rec:agent_async_ask:{interaction_id}`` so a spec reads that
    the ask ran EXACTLY ONCE (a resume substitutes the answer, never re-runs this tool).
    Returns the ``SuspendedInteraction`` so the tool seam converts it to the reserved park
    marker."""
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import (
        get_execution_identity,
        reset_execution_identity,
        set_execution_identity,
    )
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask_user

    identity_token = (
        None
        if get_execution_identity() is not None
        else set_execution_identity(
            CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-agent-fp")
        )
    )
    try:
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = await ask_user(question, mode="async", expiry_at=expiry_at)
    finally:
        if identity_token is not None:
            reset_execution_identity(identity_token)
    record = json.dumps({"pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:agent_async_ask:{suspended.interaction_id}", record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_record_identity(thread_id: str) -> dict:
    """Record the execution identity this call runs under onto
    ``e2e:rec:agent_resume:{thread_id}``.

    The agent's scripted model calls it on the RESUMED turn, so the record proves the
    resume drove REBOUND to the STORED park identity (never the answerer's or reaper's
    default ``None``) and that the run advanced past the park. The resuming pid is recorded
    too, for the cross-worker read."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    bound_identity = get_execution_identity()
    identity = bound_identity.user_id if bound_identity is not None else None
    record = json.dumps({"identity": identity, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:agent_resume:{thread_id}", record))
    return {"recorded": True}
