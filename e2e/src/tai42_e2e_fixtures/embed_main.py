"""The embed deployment shape under test: a host-owned ASGI process.

A user builds the tai MCP application with the public factory
``tai42_skeleton.asgi.create_app`` and mounts it inside their own FastAPI host,
serving the whole thing with ``uvicorn tai42_e2e_fixtures.embed_main:app``. This is
the living example of the mount-with-composed-lifespan pattern:

* ``tai = create_app()`` takes no ``manifest_path`` — the manifest resolves from
  ``TAI_MANIFEST_PATH`` in the process environment, the env-driven embed shape.
* the host lifespan enters the tai app's own lifespan via the
  ``tai42_skeleton.asgi.lifespan`` helper so the mounted app's worker startup
  (manifest load, ``app_context``, transport selection) runs under the host's
  process,
* ``GET /embed-host/ping`` is a host-owned route registered BEFORE the mount,
  proving host routes coexist with the mounted tai app,
* ``host.mount("/", tai)`` serves the tai app's ``/health``, ``/api/*``, ``/mcp``
  and ``/metrics`` paths at the paths the harness helpers expect.

The process comes up in in-process Prometheus metrics mode: nothing sets
``PROMETHEUS_MULTIPROC_DIR``, so ``/metrics`` renders the in-process registry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.applications import Starlette
from tai42_skeleton.asgi import create_app, lifespan

tai: Starlette = create_app()


@asynccontextmanager
async def host_lifespan(_host: FastAPI) -> AsyncIterator[None]:
    """Run the mounted tai app's lifespan for the duration of the host's.

    A mounted sub-app's lifespan is not driven by the parent, so the host enters
    it explicitly through the ``lifespan`` helper.
    """
    async with lifespan(tai):
        yield


host = FastAPI(lifespan=host_lifespan)


@host.get("/embed-host/ping")
async def ping() -> dict[str, str]:
    """A host-owned route beside the mounted tai app."""
    return {"host": "ok"}


host.mount("/", tai)

app = host
