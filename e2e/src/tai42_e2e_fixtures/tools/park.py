"""Async-park probe tools: the flow-driver parks, the shared multi-park super-step
barrier, the agent async-park leg, and a pausing tool that ends on a resume-buffered partial.
Each registers at import via ``@tai42_app.tools.tool`` and exposes an ``e2e_record`` Redis
observable or its return value."""

from __future__ import annotations

import json
import os
from typing import Literal

from tai42_contract.app import tai42_app
from tai42_contract.conversations import ReplyTo
from tai42_contract.interactions import ResumeBuffered
from tai42_contract.tools import TOOL_META_PAUSES

from tai42_e2e_fixtures.tools.ask_results import parked
from tai42_e2e_fixtures.tools.basic import _E2eProbeRedisSettings
from tai42_e2e_fixtures.tools.driving import driving_as

# The synthetic execution identity the park driver binds before ``ask`` and the
# async park stores as its ``continuation_identity``. A value nothing else on the stack
# produces, so a continuation that fires under it proves the stored-identity rebind (the
# auth-off default execution identity is ``None``, never this).
_ASYNC_PARK_IDENTITY = "e2e-async-driver"


def _on_expiry_policy(value: str) -> Literal["kill", "resume"]:
    """Narrow a fixture caller's ``on_expiry`` to the ask expiry policy, raising on any other value.

    The tools take the policy as a plain ``str`` (their signatures are resolved by the agent
    tool binder, which cannot evaluate a ``Literal`` forward reference), so the ask-policy type
    is recovered here and a bad value fails loudly rather than silently taking a branch.
    """
    if value == "kill":
        return "kill"
    if value == "resume":
        return "resume"
    raise ValueError(f"on_expiry must be 'kill' or 'resume', got {value!r}")


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_park_flow(
    question: str,
    expiry_seconds: float,
    on_expiry: str = "kill",
    channel: str | None = None,
    recipient: str | None = None,
) -> dict:
    """Drive an async ``ask`` park exactly as a resuming driver would.

    Binds ``e2e_async_resume`` as the current driver's resume continuation tool and drives under
    the caller's bound execution identity, binding the synthetic stand-in
    (``_ASYNC_PARK_IDENTITY``) only on an auth-off stack that binds none — the stored identity is
    what the resume rebinds as, and on an access-controlled stack only a real key still carries
    authority then. It then calls
    ``ask(mode="async", expiry_at=now+expiry_seconds, on_expiry=_on_expiry_policy(on_expiry))``,
    which PARKS and returns a ``SuspendedInteraction`` at once, never blocking, and unbinds both.
    Returns the parked ``interaction_id`` and the STORED identity so a spec can answer
    (or await the expiry of) THIS park and assert the resume ran under that stored identity.
    An answer always fires ``e2e_async_resume`` as the stored identity; on expiry
    ``on_expiry="resume"`` fires the same continuation with the expiry marker, while
    ``on_expiry="kill"`` tears the chain down and delivers the run's single FAILED — in
    whichever process resolves the park. ``channel`` and ``recipient``, when given, deliver the
    question over that channel to that recipient; without them it is answered in the inbox."""
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.authz.execution_identity import get_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    bound = get_execution_identity()
    identity = (
        None
        if bound is not None
        else CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-async-fp")
    )
    stored_identity = bound.user_id if bound is not None else _ASYNC_PARK_IDENTITY
    with driving_as(continuation="e2e_async_resume", identity=identity):
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = parked(
            await ask(
                question,
                mode="async",
                expiry_at=expiry_at,
                on_expiry=_on_expiry_policy(on_expiry),
                channel=channel,
                recipient=recipient,
            )
        )
    assert suspended.expiry_at is not None  # async park always carries its deadline
    return {
        "interaction_id": suspended.interaction_id,
        "expiry_at": suspended.expiry_at.isoformat(),
        "pid": os.getpid(),
        "stored_identity": stored_identity,
    }


@tai42_app.tools.tool(tags={"e2e"}, meta={TOOL_META_PAUSES: True})
async def e2e_chained_park(question: str, expiry_seconds: float) -> object:
    """Park an async ``ask`` under this probe's own resume continuation and RETURN the park.

    The same park ``e2e_async_park_flow`` raises (resumed by ``e2e_async_resume`` as the synthetic
    stand-in identity), but the ``SuspendedInteraction`` itself is returned, so a run that calls this
    from a chained dispatch — an agent's tool call — waits on the CALL: it parks on the chained
    resume key and the ask is a park of a run nested under it."""
    from datetime import UTC, datetime, timedelta

    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    identity = CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-async-fp")
    with driving_as(continuation="e2e_async_resume", identity=identity):
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        return await ask(question, mode="async", expiry_at=expiry_at)


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_resume(
    interaction_id: str, answer: object, reply_to: ReplyTo | None, params: dict[str, str] | None
) -> dict:
    """The generic resume continuation an async park fires ONCE — the stand-in flow
    driver's resume seam.

    Runs as the park's stored identity with ``{interaction_id, answer, reply_to, params}``: the
    ANSWER branch records the delivered answer, the EXPIRY branch (``answer`` is the generic
    ``EXPIRY_ANSWER`` marker) records that the park expired unanswered. Reads the
    execution identity it is CURRENTLY bound under — the same seam the continuation runs
    beneath (``get_execution_identity().user_id``) — and RPUSHes
    ``{"branch", "value", "reply_to", "params", "pid", "identity"}`` onto
    ``e2e:rec:async_resume:{interaction_id}`` so the spec reads which branch this park resumed
    through, the quoted message and the params the answer carried, in which process, AND under
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
    record = json.dumps(
        {
            "branch": branch,
            "value": value,
            "reply_to": None if reply_to is None else reply_to.model_dump(mode="json"),
            "params": params,
            "pid": os.getpid(),
            "identity": identity,
        }
    )
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:async_resume:{interaction_id}", record))
    return {"branch": branch}


# The N-park super-step barrier the multi-suspend leg drives. Three async parks are
# minted in ONE driver call (one logical super-step); each fires the shared
# ``e2e_async_multipark_resume`` continuation on whichever worker resolves it. The
# continuation receives only ``{interaction_id, answer, reply_to, params}`` (flow-blind), so the driver
# routes each park's interaction id to its barrier slot on the probe channel.
_MULTIPARK_ROUTE_KEY = "e2e:multipark:route"


def _multipark_barrier_key(super_step_id: str) -> str:
    return f"e2e:multipark:barrier:{super_step_id}"


def _multipark_minted_key(super_step_id: str) -> str:
    return f"e2e:multipark:minted:{super_step_id}"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_async_multipark_flow(
    super_step_id: str,
    expiry_seconds: list[float],
    slow_slot: int = -1,
    slow_seconds: float = 0.0,
    on_expiry: str = "kill",
) -> dict:
    """Drive N concurrent async ``ask`` parks in ONE super-step (one driver call).

    For each slot it binds ``e2e_async_multipark_resume`` as the resume continuation plus
    the synthetic park identity (``_ASYNC_PARK_IDENTITY``) and calls
    ``ask(mode="async", expiry_at=now+expiry_seconds[slot], on_expiry=_on_expiry_policy(on_expiry))`` — each
    PARKS and returns at once, so all N are suspended concurrently within this single call.
    A slot left to expiry fires its continuation with the expiry marker under
    ``on_expiry="resume"``, or tears the chain down under ``on_expiry="kill"``. It routes each
    park's ``interaction_id`` to its ``{super_step_id, slot, n, slow}`` on the probe channel
    so the flow-blind continuation can recover its barrier slot from the interaction id
    alone. ``slow_slot`` marks one slot whose continuation sleeps ``slow_seconds`` before
    applying, holding its due-record in flight past the reaper interval so a redelivery laps
    the original fire — the concurrent-redelivery hazard the barrier's idempotent apply must
    absorb. Returns the parked ``interaction_id`` per slot and the STORED identity."""
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    identity = CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-async-fp")
    n = len(expiry_seconds)
    interaction_ids: list[str] = []
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        for slot, expiry in enumerate(expiry_seconds):
            with driving_as(continuation="e2e_async_multipark_resume", identity=identity):
                expiry_at = datetime.now(UTC) + timedelta(seconds=expiry)
                suspended = parked(
                    await ask(
                        f"{super_step_id}:{slot}",
                        mode="async",
                        expiry_at=expiry_at,
                        on_expiry=_on_expiry_policy(on_expiry),
                    )
                )
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
async def e2e_async_multipark_resume(
    interaction_id: str, answer: object, reply_to: ReplyTo | None, params: dict[str, str] | None
) -> dict:
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
# execution identity ``ask`` needs, and only when the stack has not already bound one
# (see its docstring), then returns the ``SuspendedInteraction`` the in-process tool seam
# stamps into the park marker the agent's park middleware interrupts on.
@tai42_app.tools.tool(tags={"e2e"}, meta={TOOL_META_PAUSES: True})
async def e2e_agent_async_ask(question: str, expiry_seconds: float, on_expiry: str = "kill") -> object:
    """Async ``ask`` an agent tool call reaches, parking the agent run.

    INHERIT OR BIND. ``ask`` stores the CURRENTLY bound execution identity as the
    parked interaction's ``continuation_identity``, and the resolution door rebinds exactly
    that identity to fire the resume. So this probe binds its synthetic stand-in
    (:data:`_ASYNC_PARK_IDENTITY`) ONLY when nothing is bound — the auth-off stacks, which
    carry no caller identity and where ``ask`` would otherwise refuse the ask. Where the
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
    from tai42_skeleton.interactions import ask

    identity_token = (
        None
        if get_execution_identity() is not None
        else set_execution_identity(
            CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-agent-fp")
        )
    )
    try:
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = parked(
            await ask(question, mode="async", expiry_at=expiry_at, on_expiry=_on_expiry_policy(on_expiry))
        )
    finally:
        if identity_token is not None:
            reset_execution_identity(identity_token)
    record = json.dumps({"pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:agent_async_ask:{suspended.interaction_id}", record))
    return suspended


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_undeclared_park(question: str, expiry_seconds: float) -> object:
    """An async ``ask`` from a tool that does NOT declare ``tai42/pauses``: every door refuses its park.

    Drives the ask under whatever resume continuation and execution identity the caller already
    bound (an agent turn binds its own), binding ``e2e_async_resume`` and the synthetic identity
    only where none is bound (a direct run-tool or MCP call), so the ask parks on every door.
    RPUSHes ``{pid}`` onto ``e2e:rec:undeclared_park:{question}`` so a spec reads that the ask
    fired exactly once, and returns the ``SuspendedInteraction`` the platform refuses by name."""
    from collections.abc import Awaitable
    from contextlib import ExitStack
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_contract.interactions import get_resume_continuation_tool
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.authz.execution_identity import (
        get_execution_identity,
        reset_execution_identity,
        set_execution_identity,
    )
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    identity = (
        None
        if get_execution_identity() is not None
        else CallerIdentity(user_id=_ASYNC_PARK_IDENTITY, execution_key_fingerprint="e2e-undeclared-fp")
    )
    with ExitStack() as binding:
        if get_resume_continuation_tool() is None:
            binding.enter_context(driving_as(continuation="e2e_async_resume", identity=identity))
        elif identity is not None:
            identity_token = set_execution_identity(identity)
            binding.callback(reset_execution_identity, identity_token)
        expiry_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)
        suspended = parked(await ask(question, mode="async", expiry_at=expiry_at))
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(
            Awaitable[int], client.rpush(f"e2e:rec:undeclared_park:{question}", json.dumps({"pid": os.getpid()}))
        )
    return suspended


@tai42_app.tools.tool(tags={"e2e"}, meta={TOOL_META_PAUSES: True})
async def e2e_resume_buffered(key: str, mark: str, buffer: bool, remaining_ids: list[str]) -> object:
    """End on a ``ResumeBuffered`` partial when ``buffer`` is set, else on a plain result.

    A ``ResumeBuffered`` is what a resume that answered one ask of several returns: the step's
    other asks (``remaining_ids``) are still open, so the run stays parked. The plain result
    ``{"key": key, "mark": mark}`` is the control a spec compares the partial against. ``key``
    and ``mark`` are read by a binding's subject expression and update, never by the tool.
    """
    if buffer:
        return ResumeBuffered(remaining_ids=remaining_ids)
    return {"key": key, "mark": mark}


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


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_expire_park(interaction_id: str) -> dict:
    """Bring a live async park to its deadline NOW, so the running expiry reaper resolves it next pass.

    Reschedules the park's ``expiry_at`` into the immediate past — rewriting the durable
    interaction state AND its score in the per-interaction expiry index the reaper scans — so
    the deadline stops riding real wall-clock. A spec parks with a deadline far enough out that
    the sync submit always observes the suspend first, then calls this to reach expiry without a
    short park window racing the submit latency; the reaper's own claim then fires the stored
    continuation deterministically.

    Writes the past deadline into the state BEFORE re-scoring the index, so the reaper that reads
    the freshly-due index member always sees the already-passed deadline and fires on that same
    pass. Raises loudly when ``interaction_id`` names no live async park (already resolved,
    vanished, or never a park), so a mis-sequenced spec fails visibly rather than silently no-opping.
    """
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.interactions import InteractionStore, interactions_settings

    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
        if (
            state is None
            or state.status != "pending"
            or state.request.mode != "async"
            or state.request.expiry_at is None
        ):
            raise RuntimeError(f"e2e_expire_park: {interaction_id!r} is not a live async park to expire")
        past = datetime.now(UTC) - timedelta(seconds=1)
        expired_request = state.request.model_copy(update={"expiry_at": past})
        await cast(
            Awaitable[int], r.hset(store.state_key(interaction_id), "request", expired_request.model_dump_json())
        )
        await cast(Awaitable[int], r.zadd(store.pending_expiry_key, {interaction_id: int(past.timestamp() * 1000)}))
    return {"interaction_id": interaction_id, "expiry_at": past.isoformat()}


# Every key a park writes takes a TTL in whole seconds, rounded up from the moment of its own
# write, so keys sized to the same horizon end within one second of each other.
_SAME_HORIZON_MS = 1000


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_lapse_park(interaction_id: str) -> dict:
    """Age a live async park out past its retention horizon, as Redis would, so the reaper reaches a lapse.

    Deletes exactly the keys Redis expires when the park's state hash does: of every key the park
    wrote (its state hash and callback tickets, the group stream and pending count, the group's
    media index and media, the thread-parks, run-parks and subject sets), each whose TTL ends no
    later than the state hash's. A shared key a sibling park keeps alive past that stays whole,
    the lapsed park's member included, just as a real lapse leaves it; a key with no TTL never
    lapses. The expiry-index member stays behind, re-scored into the immediate past in the same
    transaction: a park lapses only after its deadline, so a lapsed park's member is always
    already due, and the running reaper reaches it next pass with no pending park behind it.
    Raises loudly when ``interaction_id`` names no live async park (already resolved, vanished,
    or never a park), or when its state hash carries no TTL to lapse by, so a mis-sequenced spec
    fails visibly.
    """
    from collections.abc import Awaitable
    from datetime import UTC, datetime, timedelta
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient
    from tai42_skeleton.interactions import InteractionStore, interactions_settings
    from tai42_skeleton.interactions.store.records import iter_subject_keys

    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
        if (
            state is None
            or state.status != "pending"
            or state.request.mode != "async"
            or state.request.expiry_at is None
        ):
            raise RuntimeError(f"e2e_lapse_park: {interaction_id!r} is not a live async park to lapse")
        fields = await cast(Awaitable[dict], r.hgetall(store.state_key(interaction_id)))

        def _text(value: str | bytes) -> str:
            return value.decode() if isinstance(value, bytes) else value

        def _field(name: str) -> str | None:
            value = fields.get(name, fields.get(name.encode()))
            return None if value is None else _text(value)

        group_id = state.request.group_id
        keys = [store.state_key(interaction_id), store.group_key(group_id), store.count_key(group_id)]
        keys += [
            key
            async for key in r.scan_iter(match=store.ticket_key("*"))
            if (await cast(Awaitable[str | bytes | None], r.get(key))) in (interaction_id, interaction_id.encode())
        ]
        media_index = store.media_index_key(group_id)
        keys.append(media_index)
        keys += [store.media_key(_text(media_id)) for media_id in await cast(Awaitable[set], r.smembers(media_index))]
        thread_id = _field("thread_id")
        if thread_id is not None:
            keys.append(store.thread_parks_key(thread_id))
        run_delivery_id = _field("run_delivery_id")
        if run_delivery_id is not None:
            keys.append(store.run_parks_key(run_delivery_id))
        subjects = _field("subjects")
        if subjects is not None:
            for target_kind, target_name, kind, key in iter_subject_keys(json.loads(subjects)):
                keys.append(store.subject_parks_key(target_kind, target_name, kind, key))
                keys.append(store.subject_scopes_key(kind, key))
        keys = list(dict.fromkeys(keys))

        read = r.pipeline(transaction=False)
        for key in keys:
            read.pttl(key)
        ttls = await read.execute()
        state_ttl = ttls[0]
        if state_ttl <= 0:
            raise RuntimeError(f"e2e_lapse_park: {interaction_id!r} state has no TTL to lapse by (PTTL {state_ttl})")
        lapsed = [key for key, key_ttl in zip(keys, ttls, strict=True) if 0 < key_ttl <= state_ttl + _SAME_HORIZON_MS]

        past = datetime.now(UTC) - timedelta(seconds=1)
        pipe = r.pipeline(transaction=True)
        pipe.delete(*lapsed)
        pipe.zadd(store.pending_expiry_key, {interaction_id: int(past.timestamp() * 1000)})
        await pipe.execute()
    return {"interaction_id": interaction_id, "expiry_at": past.isoformat(), "keys_lapsed": len(lapsed)}
