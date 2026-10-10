"""A route that leases the pooled Redis client once a reload has advanced the client epoch.

Imported by a served manifest's ``routers_modules``. A request to it is admitted by the
generation serving it and waits until a reload advances the epoch, so its lease opens a
connection in the NEXT epoch's pool: the connection outlives the generation that served
the request.
"""

from __future__ import annotations

import asyncio
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from tai42_contract.app import tai42_app
from tai42_kit.clients import client_ctx, current_client_epoch
from tai42_kit.clients.impl.redis import RedisClient

from .pooled_store_names import POOLED_STORE_PATH, POOLED_STORE_URL_ENV

# How long the request waits for a reload before it fails: room for a reload with re-imports
# on a loaded host, inside the 30 s the test's client waits for the response.
_RELOAD_WAIT_SECONDS = 25.0


@tai42_app.http.custom_route(
    POOLED_STORE_PATH,
    methods=["GET"],
    summary="Lease the pooled Redis client after the next reload.",
    tags=["test"],
    response_model=None,
    no_body_reason="test fixture: response body not under test",
    authed=False,
)
async def pooled_store_probe(_request: Request) -> JSONResponse:
    admitted_epoch = current_client_epoch()
    # A deadline checked between short sleeps, not ``asyncio.timeout``: a cancelled timeout's
    # timer stays scheduled until its deadline, carrying this request's context past the
    # test's wait for the retired generation to be released.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RELOAD_WAIT_SECONDS
    while current_client_epoch() == admitted_epoch:
        if loop.time() >= deadline:
            raise TimeoutError(f"no reload advanced the client epoch within {_RELOAD_WAIT_SECONDS} s")
        await asyncio.sleep(0.01)
    async with client_ctx(RedisClient, url=os.environ[POOLED_STORE_URL_ENV]) as redis:
        pong = await redis.ping()
    return JSONResponse({"admitted_epoch": admitted_epoch, "pong": bool(pong)})
