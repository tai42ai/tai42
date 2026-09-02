"""Probe tools the harness observes the system under test through.

Each registers at import via ``@tai42_app.tools.tool`` and is selected by a
``tools:`` manifest entry. Probes never mock — they expose an in-process
observable (a return value or an ``e2e_record`` Redis side effect) so a test can
read what actually happened inside a real server/worker process."""

from __future__ import annotations

import json
import os
import socket

from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.secrets import SecretValue
from tai42_kit.clients import RedisConnectionSettings, current_client_epoch
from tai42_kit.settings import KeyMaterial, TaiBaseSettings


class _E2eProbeRedisSettings(RedisConnectionSettings):
    """Points the probe-record client at the harness probe channel (logical DB
    0) via ``E2E_PROBE_REDIS_URL``."""

    model_config = SettingsConfigDict(env_prefix="E2E_PROBE_")


class E2eProbeSecretSettings(TaiBaseSettings):
    """A registered ``key_material`` field the settings-profile door suite drives the
    key_material refusal against on every leg (this module ships in every stack's
    ``tools:`` entry). It is ``hot`` (the class default) and thus NOT X-band, so a
    profile naming ``E2E_PROBE_SECRET_KEY_MATERIAL`` exercises the DISTINCT key_material
    refusal rather than the X-band one. Unset by default — no stack sets it, so it never
    enters a stored-env payload."""

    model_config = SettingsConfigDict(env_prefix="E2E_PROBE_SECRET_")

    key_material: KeyMaterial | None = None


@tai42_app.tools.tool(tags={"e2e"})
def e2e_echo(payload: str) -> str:
    """Return ``payload`` unchanged — the default subject for extension
    attachment (prometheus / batch / proxy branches)."""
    return payload


@tai42_app.tools.tool(tags={"e2e"})
def e2e_secret_value(credential: str) -> dict:
    """Return ``credential`` wrapped in a ``SecretValue`` envelope — the probe for
    the secret-value doors. The live sync run-tool door and a genuine MCP call
    reveal the real value; a background tool-run records the masked placeholder."""
    return {"credential": SecretValue(credential)}


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_worker_info() -> dict:
    """Report identity + metrics/socket state + a state digest of the process that
    ran this call.

    ``name`` + ``generation`` are this process's bus identity — the stable slot name it
    holds for one claim's life and the monotonic life counter minted with that claim
    (read off the in-process worker bus, distinct from the settings epoch).

    ``value_class`` is the frozen ``prometheus_client`` value backend —
    ``MmapedValue`` when the multiproc env froze correctly, ``MutexValue`` when
    it did not (the C2 in-vivo probe). ``socket_class`` doubles as the C8
    pristine-socket check (a proxy leak would leave a non-stdlib socket class).

    ``state_digest`` is a PROBE-COMPUTED hash of this process's OWN live view —
    ``tai42_app.admin.live_manifest`` plus its sorted live tool names, computed
    in-process. It is not a SUT-emitted revision marker (none exists): convergence
    is every distinct pid reporting the SAME digest, differing from a pre-mutation
    baseline, so a test never recomputes an expected digest from the seeded file
    (the live view is defaults-merged and ``!ENV``-resolved)."""
    import hashlib

    import prometheus_client.values
    from tai42_skeleton.app import instance

    value_class = getattr(prometheus_client.values.ValueClass, "__name__", repr(prometheus_client.values.ValueClass))
    tool_names = sorted(await tai42_app.tools.get_tools())
    material = json.dumps({"manifest": tai42_app.admin.live_manifest, "tools": tool_names}, sort_keys=True, default=str)
    state_digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    identity = instance.app.bus.identity
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "name": identity.name,
        "generation": identity.generation,
        "value_class": value_class,
        "multiproc_dir_env": os.environ.get("PROMETHEUS_MULTIPROC_DIR"),
        "socket_class": f"{socket.socket.__module__}.{socket.socket.__qualname__}",
        "state_digest": state_digest,
    }


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_settings_snapshot() -> dict:
    """Report this process's live settings-epoch posture — the observable the profile
    flip test reads to certify a hot config apply converged with no leak.

    Walks ``registered_settings()`` and resolves each field's EFFECTIVE value from the
    live process env (the precedence a freshly-constructed singleton reads under — a
    reset cache re-reads it), masking ``secret`` / ``key_material`` fields (value ``None``,
    only a ``set`` flag). ``settings_epoch`` and ``client_pool_epoch`` are the ONE shared
    process counter (``current_client_epoch``). ``stale_holders`` is the sanctioned
    :func:`sweep_stale_settings` run over every RETIRED epoch — a settings instance of a
    retired epoch a holder still keeps alive; a clean flip leaves ZERO. ``stale_pool_epochs``
    are retired client-pool epochs still present (a clean retire drains + detaches them);
    ``pool_leases_by_epoch`` is the live lease count per pooled epoch. Probes never mock:
    every field is read off in-process live state."""
    import tai42_kit.clients.base as clients_base
    from tai42_kit.settings import registered_settings
    from tai42_kit.settings.cache_registry import sweep_stale_settings

    # Settings + client pools share ONE monotonic counter; read it once.
    current = current_client_epoch()

    # Stale settings holders: sweep every retired epoch (< current) through the
    # sanctioned sweep. ``sweep_stale_settings`` rejects the current/future epoch, so the
    # range stops one short of ``current``.
    stale_holders: list[dict] = []
    for retired in range(current):
        for holder in sweep_stale_settings(retired):
            stale_holders.append(
                {"settings_type": holder.settings_type, "epoch": holder.epoch, "holders": list(holder.holders)}
            )

    # Client-pool leases per epoch across every pooled loop. ``current_client_epoch`` and
    # the pool maps share the same non-reentrant lock, so ``current`` is read ABOVE, never
    # inside this block. Reaching the module globals is the only pool observable there is.
    leases_by_epoch: dict[int, int] = {}
    entries_by_epoch: dict[int, int] = {}
    with clients_base._registry_lock:
        for per_loop in clients_base._loop_clients.values():
            for epoch, per_epoch in per_loop.items():
                for clients in per_epoch.values():
                    for entry in clients.values():
                        leases_by_epoch[epoch] = leases_by_epoch.get(epoch, 0) + entry.leases
                        entries_by_epoch[epoch] = entries_by_epoch.get(epoch, 0) + 1
    stale_pool_epochs = sorted(epoch for epoch in entries_by_epoch if epoch < current)

    groups: list[dict] = []
    for info in registered_settings():
        fields: list[dict] = []
        for field in info.fields:
            masked = field.secret or field.key_material
            is_set = bool(field.env_var and field.env_var in os.environ)
            value = None if masked or not field.env_var else os.environ.get(field.env_var, field.default)
            fields.append(
                {
                    "name": field.name,
                    "env_var": field.env_var,
                    "reload_class": field.reload_class,
                    "secret": field.secret,
                    "key_material": field.key_material,
                    "set": is_set,
                    "value": value,
                    "default": None if masked else field.default,
                }
            )
        groups.append(
            {
                "name": info.name,
                "module": info.module,
                "qualname": info.qualname,
                "reload_class": info.reload_class,
                "fields": fields,
            }
        )

    return {
        "pid": os.getpid(),
        "settings_epoch": current,
        "client_pool_epoch": current,
        "stale_holder_count": len(stale_holders),
        "stale_holders": stale_holders,
        "pool_leases_by_epoch": {str(epoch): count for epoch, count in sorted(leases_by_epoch.items())},
        "stale_pool_epochs": stale_pool_epochs,
        "groups": groups,
    }


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_record(key: str, value: str) -> str:
    """RPUSH ``{"value", "pid"}`` onto the Redis list ``e2e:rec:{key}`` so the
    harness (which reads that list) sees a side effect from whichever process
    executed the call — webhook/hook/backend-execution observation."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({"value": value, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        # The pooled client yields an async Redis; rpush returns an awaitable.
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))
    return "recorded"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_sleep(seconds: float) -> str:
    """Sleep ``seconds`` (bounded at 30 so a mis-scripted test cannot hang) —
    the target of background tool-run tests."""
    import asyncio

    if seconds > 30:
        raise ValueError(f"e2e_sleep bound exceeded: {seconds} > 30 seconds")
    await asyncio.sleep(seconds)  # noqa: TID251 — the observed work under test, bounded above
    return "slept"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_slow_task(key: str, seconds: float) -> str:
    """RPUSH a ``started`` marker onto ``e2e:rec:{key}`` then sleep ``seconds``
    (bounded at 30). The worker-crash spec attaches ``sync_task`` to this, runs it
    through the tool-runs API, waits for the marker to prove the run is in-flight
    IN the backend worker, then SIGKILLs the worker — so the terminal it reads
    back is caused by the crash, not by a self-completing sleep."""
    import asyncio
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    if seconds > 30:
        raise ValueError(f"e2e_slow_task bound exceeded: {seconds} > 30 seconds")
    record = json.dumps({"value": "started", "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))
    await asyncio.sleep(seconds)  # noqa: TID251 — the observed in-flight work, bounded above
    return "done"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_drain_probe(key: str, seconds: float) -> str:
    """RPUSH a ``started`` marker, sleep ``seconds`` (bounded at 30), then RPUSH a ``done``
    marker — both onto ``e2e:rec:{key}`` with this process's pid.

    The recycle-drain spec attaches ``sync_task`` and runs it INTO the backend: after the
    ``started`` marker proves it is in-flight, the backend is recycled. Whether ``done`` also
    lands tells the test if the in-flight backend job DRAINED to completion during the
    graceful recycle shutdown (both markers) or was lost (only ``started``) — the direct
    observable of the arq job-completion-wait behavior, independent of the serve-side
    tool-run record (which a serve recycle legitimately cancels)."""
    import asyncio
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    if seconds > 30:
        raise ValueError(f"e2e_drain_probe bound exceeded: {seconds} > 30 seconds")
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", json.dumps({"value": "started", "pid": os.getpid()})))
        await asyncio.sleep(seconds)  # noqa: TID251 — the observed in-flight work, bounded above
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", json.dumps({"value": "done", "pid": os.getpid()})))
    return "done"


@tai42_app.tools.tool(tags={"e2e"})
def e2e_fail(message: str) -> str:
    """Always raise — the error-counter / is-error path."""
    raise ValueError(message)


@tai42_app.tools.tool(tags={"e2e"})
def e2e_external_link(callback_url: str) -> str:
    """Build and return an external action URL embedding ``callback_url`` — the
    subject the ``ask_external`` extension wraps. The extension supplies
    ``callback_url`` (the platform-minted ticket URL) and drives the human-in-the-
    loop external ask; this tool's job is only to weave it into the link the human
    would visit."""
    return f"https://ext.example/act?cb={callback_url}"


@tai42_app.tools.tool(tags={"e2e"})
def e2e_http_probe(url: str) -> dict:
    """GET ``url`` with a fresh env-ignoring client. Its ``_proxy`` branch tunnels
    through the harness CONNECT proxy at the socket layer; the plain tool must
    reach the target directly."""
    import httpx

    with httpx.Client(trust_env=False, timeout=10.0) as client:
        response = client.get(url)
    return {"status": response.status_code, "body_head": response.text[:200]}


@tai42_app.tools.tool(tags={"e2e"})
async def run_tool(tool_name: str, arguments: dict[str, object]) -> object:
    """Dispatch any registered tool by name — the e2e backend-execution door.

    The skeleton ``run_tool`` operation is a tier-1 META-EXECUTOR hardcode-blocked
    from the MCP surface, so it has no projected base for the ``sync_task`` backend
    extension to wrap. This fixture provides an MCP-callable dispatch-by-name whose
    ``sync_task`` branch (``run_tool_sync_task``, attached in ``_probe_tools_entry``)
    runs the dispatch INSIDE the backend worker — the "run tool X in the backend"
    vehicle the cross-worker / preset / schedule specs drive. It mirrors the skeleton
    ``run_tool`` operation's ``(tool_name, arguments)`` signature, delegating to the
    same internal registry dispatch."""
    return await tai42_app.tools.run_tool(tool_name, arguments)


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


# The process-level live-session registry the sandbox probe drives a multi-step lifecycle
# through: ``create`` returns a ``session_id`` a later ``exec`` / ``put`` / ``get`` addresses.
# A session lives in ONE process, so the sandbox-probe suites ride the single-worker
# ``build_sandbox_stack`` where every probe call lands in the same server process.
_SANDBOX_SESSIONS: dict[str, object] = {}

# The default digest-pinned image reference the probe requests (inert under the fake/local
# providers, but the model demands a digest, never a bare tag).
_PROBE_IMAGE = "img@sha256:" + "0" * 64


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_sandbox_probe(
    op: str,
    *,
    session_id: str | None = None,
    workspace_key: str = "e2e-probe",
    durability: str = "ephemeral",
    network: str = "egress",
    image: str = _PROBE_IMAGE,
    argv: list[str] | None = None,
    path: str | None = None,
    data: str | None = None,
    ttl_seconds: int = 300,
    timeout_seconds: float = 30,
    record_key: str | None = None,
) -> dict:
    """Drive one sandbox lifecycle step through the ONE acquisition chokepoint.

    Acquires the provider via ``tai42_app.sandboxes.require_sandbox()`` (so a provider-less
    stack surfaces the typed loud ``SandboxUnavailableError`` through this tool's OWN
    error path — never a 501 route) and performs ``op``:
    ``create`` / ``exec`` / ``put`` / ``get`` / ``touch`` / ``info`` / ``list`` / ``reap`` /
    ``destroy``. The outcome is returned AND, when ``record_key`` is set, RPUSHed onto
    ``e2e:rec:{record_key}`` so a spec reads it back through ``stack.records``.
    """
    from tai42_contract.sandbox import SandboxSessionSpec

    sandbox = tai42_app.sandboxes.require_sandbox()

    if op == "create":
        spec = SandboxSessionSpec(
            image=image,
            workspace_key=workspace_key,
            durability=durability,  # type: ignore[arg-type]  # validated by the model
            network=network,  # type: ignore[arg-type]  # validated by the model
            ttl_seconds=ttl_seconds,
        )
        session = await sandbox.create_session(spec)
        _SANDBOX_SESSIONS[session.id] = session
        outcome = {"session_id": session.id, "workspace_path": session.workspace_path}
    elif op == "list":
        infos = await sandbox.list_sessions()
        outcome = {"sessions": [info.id for info in infos]}
    elif op == "reap":
        outcome = {"reaped": await sandbox.reap()}
    else:
        session = _require_probe_session(session_id)
        if op == "exec":
            result = await session.exec(argv or [], timeout_seconds=timeout_seconds)  # type: ignore[attr-defined]
            outcome = {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
        elif op == "put":
            await session.put_file(_require(path, "path"), (data or "").encode("utf-8"))  # type: ignore[attr-defined]
            outcome = {"put": path}
        elif op == "get":
            payload = await session.get_file(_require(path, "path"))  # type: ignore[attr-defined]
            outcome = {"data": payload.decode("utf-8", "replace")}
        elif op == "touch":
            await session.touch()  # type: ignore[attr-defined]
            outcome = {"touched": True}
        elif op == "info":
            info = await session.info()  # type: ignore[attr-defined]
            outcome = {"session_id": info.id, "workspace_path": info.workspace_path, "durability": info.durability}
        elif op == "destroy":
            await session.destroy()  # type: ignore[attr-defined]
            _SANDBOX_SESSIONS.pop(session_id or "", None)
            outcome = {"destroyed": True}
        else:
            raise ValueError(f"unknown sandbox probe op {op!r}")

    if record_key is not None:
        await _record_probe(record_key, outcome)
    return {"op": op, "pid": os.getpid(), **outcome}


def _require_probe_session(session_id: str | None) -> object:
    if session_id is None:
        raise ValueError("this sandbox probe op requires a session_id from a prior create")
    session = _SANDBOX_SESSIONS.get(session_id)
    if session is None:
        raise ValueError(f"no live probe session {session_id!r} in this process")
    return session


def _require(value: str | None, name: str) -> str:
    if value is None:
        raise ValueError(f"this sandbox probe op requires {name!r}")
    return value


async def _record_probe(key: str, outcome: dict) -> None:
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({"value": outcome, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_sandbox_attribution_probe(
    tags: list[str], metadata: dict, *, inner_tool: str = "e2e_echo", inner_arguments: dict | None = None
) -> dict:
    """Deposit a ``RunAttribution`` on the ambient ContextVar, then run a tool through the
    shared run chokepoint so its stamp is observable on the fixture monitoring backend.

    The chokepoint (``ToolBinding.run_tool``) wraps the inner dispatch in ``attribute_run``, so
    a span the inner tool opens WHILE the deposited attribution is live is recorded
    attribution-stamped on ``e2e:rec:monitor_spans`` (and the block itself on
    ``e2e:rec:trace_attrs``). ``inner_tool`` defaults to the trivial echo; a suite passes a
    span-opening tool to observe the stamped span."""
    from tai42_contract.monitoring import RunAttribution
    from tai42_skeleton.tools.attribution import run_attribution

    attribution = RunAttribution(tags=list(tags), metadata=dict(metadata))
    with run_attribution(attribution):
        result = await tai42_app.tools.run_tool(inner_tool, inner_arguments or {"payload": "attributed"})
    return {
        "tags": list(tags),
        "metadata": dict(metadata),
        "inner_tool": inner_tool,
        "result": result,
        "pid": os.getpid(),
    }


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_resolve_connector(connection_id: str, provider_id: str = "e2e_idp", sub_service: str = "default") -> dict:
    """Resolve a managed-connector token for ``connection_id`` in this process.

    Drives the real resolver path (freshness gate + refresh-under-lock + CAS
    write-back); an expired stored token forces one refresh serialized by the
    cross-process connection lock. Two concurrent calls across replicas therefore
    trigger exactly one upstream refresh — the seam the refresh-lock test reads
    off the stub IdP's refresh counter."""
    from tai42_skeleton.connectors.runtime.resolver import resolve_managed_auth

    auth = await resolve_managed_auth(connection_id, provider_id, sub_service)
    return {"resolved": auth is not None, "pid": os.getpid()}
