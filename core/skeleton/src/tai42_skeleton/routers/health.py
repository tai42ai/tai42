"""``/health`` (liveness) and ``/ready`` (readiness) custom routes.

``/health`` returns a static ``OK`` — pure liveness.

``/ready`` pings exactly the backing stores THIS deployment has wired, as each
subsystem declares them on the app's readiness registry
(:mod:`tai42_skeleton.app.readiness`; the gate deciding whether a store is wired
stays with its subsystem). A worker
whose Redis/Postgres is unreachable 500s every real request while ``/health``
stays green; ``/ready`` lets an orchestrator rotate it and a load balancer drain
it. Distinct connections are deduped and pinged once, concurrently, each under a
module-constant timeout budget.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from tai42_contract.app import tai42_app
from tai42_kit.clients import ClientSettings, client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app import instance

logger = logging.getLogger(__name__)

# Wall-clock budget for a single readiness ping (connect + command). A module
# constant, deliberately NOT a settings knob — ``/ready`` adds no config surface.
_READINESS_TIMEOUT_SECONDS = 5.0


class ReadinessStatus(BaseModel):
    """The readiness probe's raw (non-enveloped) body.

    ``checks`` maps each wired subsystem to ``"ok"`` or, on failure, the raised
    exception's TYPE name only — never its message, which would leak internal
    hosts/ports.
    """

    status: str
    checks: dict[str, str]


@tai42_app.http.custom_route(
    "/health",
    methods=["GET"],
    summary="Liveness probe",
    tags=["health"],
    response_model=None,
    no_body_reason="Liveness probe: text/plain",
    authed=False,
)
async def health_check(request):
    """Answer a liveness probe with ``OK`` (no auth, text/plain)."""
    return PlainTextResponse("OK")


async def _ping_redis(settings: ClientSettings) -> None:
    async with client_ctx(RedisClient, settings) as r:
        # redis-py types the async ``ping`` with the sync ``bool`` return, so pyright
        # sees the awaited value as non-awaitable; it is a coroutine at runtime.
        await r.ping()  # pyright: ignore[reportGeneralTypeIssues]


async def _ping_postgres(settings: ClientSettings) -> None:
    async with client_ctx(PostgresClient, settings) as pool, pool.connection() as conn:
        await conn.execute("SELECT 1")


# The per-store-type readiness ping, dispatched by the wired connection's client class. A class not
# listed here has no ping and is a loud wiring bug, never a Postgres fallback.
_PING_BY_CLIENT: dict[type, Callable[[ClientSettings], Awaitable[None]]] = {
    RedisClient: _ping_redis,
    PostgresClient: _ping_postgres,
}


async def _ping_connection(client_cls: type, settings: ClientSettings) -> Exception | None:
    """Ping one distinct connection under the readiness timeout.

    Returns ``None`` on success, or the raised exception on failure. The failure
    is never swallowed: it is logged in full here (one warning per failed
    connection, with the traceback) and returned so the response can carry the
    exception TYPE only — the message would leak internal hosts/ports.
    """
    # Resolve the ping for this store type BEFORE the try. A type the probe has no ping for is a
    # wiring bug, not a store outage — surface it loudly here rather than ping it as Postgres (a
    # silent catch-all else would both mis-probe it and hide the bug).
    ping = _PING_BY_CLIENT.get(client_cls)
    if ping is None:
        raise TypeError(f"readiness probe has no ping for client type {client_cls.__name__!r}")
    try:
        async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
            await ping(settings)
    except Exception as exc:  # reported to the caller and logged with detail — never swallowed
        logger.warning("readiness ping failed for %s", client_cls.__name__, exc_info=True)
        return exc
    return None


@tai42_app.http.custom_route(
    "/ready",
    methods=["GET"],
    summary="Readiness probe",
    tags=["health"],
    response_model=ReadinessStatus,
    enveloped=False,
    authed=False,
)
async def readiness_check(request: Request) -> JSONResponse:
    """Readiness probe: ping every backing store this deployment has wired.

    Pings exactly the Redis/Postgres connections the gated-in subsystems use,
    deduped by pool identity so subsystems sharing one pool are pinged ONCE, and
    each distinct pool concurrently under a 5s budget. All pass -> 200
    ``{"status": "ready", "checks": {name: "ok"}}``; any fail -> 503
    ``{"status": "not_ready", ...}`` whose failing checks carry only the exception
    TYPE name — never the message, which would leak internal hosts/ports (full
    detail is logged server-side, one warning per failed connection). A deployment
    with nothing gated in returns 200 with empty checks.

    Mapped public by the operator like ``/health`` (there is no code-side public
    list — ``ResourceGuardMiddleware`` denies unknown routes). Being declared public,
    the probe passes the gate without reading the gate's own Redis, so that Redis is
    pinged here as one of the ``access_control`` row's targets.
    """
    app = instance.build_app()
    targets = app.readiness.wired_targets()

    # Dedupe by (client class, pool identity): the client's own pool-identity key is
    # what decides whether two settings share one pool, so subsystems that resolve to
    # the same pool are pinged once and every subsystem on it reports that one result.
    # Two Redis subsystems on an explicit TAI_DEFAULT_REDIS_URL, or two Postgres
    # subsystems whose settings prefixes differ but resolve to one DSN, dedupe to a
    # single ping; two distinct DSNs (or URLs) stay two pings.
    distinct: dict[tuple[type, str], tuple[type, ClientSettings]] = {}
    subsystem_keys: dict[str, list[tuple[type, str]]] = {}
    for name, client_cls, settings in targets:
        key = (client_cls, client_cls().pool_key(**settings.client_kwargs()))
        distinct.setdefault(key, (client_cls, settings))
        subsystem_keys.setdefault(name, []).append(key)

    keys = list(distinct)
    results = await asyncio.gather(*(_ping_connection(*distinct[key]) for key in keys))
    outcome: dict[tuple[type, str], Exception | None] = dict(zip(keys, results, strict=True))

    checks: dict[str, str] = {}
    ready = True
    for name, keys_for_name in subsystem_keys.items():
        failure = next((outcome[key] for key in keys_for_name if outcome[key] is not None), None)
        if failure is None:
            checks[name] = "ok"
        else:
            checks[name] = type(failure).__name__
            ready = False

    # A perpetual background task that died (a subscription/re-probe/reaper that
    # stopped) marks the live app; surface it as a named readiness failure so this
    # worker drains. The process also requests its own graceful exit when the task
    # dies, so this window is the pre-exit truth.
    dead = app.dead_perpetual_task()
    if dead is not None:
        name, reason = dead
        checks[f"perpetual_task:{name}"] = reason
        ready = False

    if ready:
        return JSONResponse({"status": "ready", "checks": checks})
    return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
